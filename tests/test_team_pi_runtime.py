from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import Mock, patch

from rag_ime.agent_events import AgentEventHub
from rag_ime.agent_runtime_driver import AgentRuntimeError, RuntimeDriverContext
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.pi.factory import PiRuntimeDriverFactory
from rag_ime.pi.runtime import PiRuntimeHostManager
from rag_ime.pi.values import PiRuntimeError
from rag_ime.team.execution import ExecutionReceipt, ExecutionSpec
from rag_ime.team.runtime import TeamRuntimeBinding, TeamRuntimeDriver
from rag_ime.team.shared_packages import StagedTeamPackage


class TeamRuntimeBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-team-runtime-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.host = self.root / "host"
        self.host.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "log_path=pathlib.Path(os.environ['RAG_IME_PI_AGENT_DIR']) / 'host-requests.jsonl'\n"
            "for line in sys.stdin:\n"
            " request=json.loads(line)\n"
            " log_path.parent.mkdir(parents=True, exist_ok=True)\n"
            " with log_path.open('a', encoding='utf-8') as handle: handle.write(json.dumps(request) + '\\n')\n"
            " result={}\n"
            " if request['method']=='hello': result={'capabilities':{'multiSession':True,'maxSessions':1,'sessionControlState':True}}\n"
            " elif request['method']=='session.open': result={'snapshot':{'sessionId':request['params']['sessionId'],'piSessionId':request['params']['sessionId'],'sessionFile':request['params'].get('sessionFile') or '/run/paw/sessions/' + request['params']['sessionId'] + '.jsonl','leafId':'','isIdle':True,'model':{'provider':'test','id':'test-model'}}}\n"
            " elif request['method']=='session.control_state': result={'schemaVersion':'rag-ime.pi-session-control-state.v1','sessionId':request['params']['sessionId'],'isIdle':True}\n"
            " print(json.dumps({'id':request['id'],'ok':True,'result':result}),flush=True)\n"
        )
        self.host.chmod(0o755)
        self.store = AgentSessionStore(self.root / "sessions.sqlite")
        self.store.initialize()
        self.first = self.store.create(title="one", created_at_ms=1)
        self.second = self.store.create(title="two", created_at_ms=2)
        self.events = AgentEventHub(sequence_loader=self.store.max_event_sequence)
        self.config = PiRuntimeConfig(
            enabled=True,
            executable=self.host,
            agent_dir=self.root / "shared-agent",
            session_dir=self.root / "shared-sessions",
            logs_dir=self.root / "shared-logs",
            provider="test",
            model="test-model",
            provider_environment={"DEEPSEEK_API_KEY": "must-not-leak"},
            command_timeout_seconds=3,
            idle_timeout_seconds=0,
        )

    def _binding(self, session_id: str) -> TeamRuntimeBinding:
        scope = self.root / "attempts" / session_id
        workspace = scope / "workspace"
        workspace.mkdir(parents=True)
        spec = ExecutionSpec(
            attempt_id=f"attempt-{session_id}",
            session_id=session_id,
            scope_root=scope,
            workspace_root=workspace,
            agent_dir=scope / "agent",
            session_dir=scope / "sessions",
            logs_dir=scope / "logs",
            home_dir=scope / "home",
            tmp_dir=scope / "tmp",
            container_id=f"paw-{session_id.split(':')[-1]}",
            runtime_image="local-test",
            runtime_command=(str(self.host),),
        )
        return TeamRuntimeBinding(
            spec=spec,
            gateway_url="http://127.0.0.1:9999/broker",
            gateway_token=f"grant-{session_id}",
            grant_id=f"grant-{session_id}",
            launcher=_LocalLauncher(),
        )

    def test_factory_returns_per_session_managers_with_scoped_config(self) -> None:
        bindings = {self.first["id"]: self._binding(self.first["id"]), self.second["id"]: self._binding(self.second["id"])}
        for session in (self.first, self.second):
            binding = bindings[session["id"]]
            self.store.set_runtime_policy(
                session["id"],
                mode="coordinator",
                tool_profile_version="control-center-v1",
                allowed_tools=None,
                project_context_enabled=False,
                workspace_roots=[str(binding.spec.workspace_root)],
            )
        factory = PiRuntimeDriverFactory(
            self.config,
            execution_binding_resolver=lambda session: bindings[str(session["id"])],
        )
        context = RuntimeDriverContext(
            sessions=self.store,
            events=self.events,
            tool_gateway_token="process-token",
        )
        driver = factory.create(context, purpose="interactive")
        self.addCleanup(driver.stop)
        self.assertIsInstance(driver, TeamRuntimeDriver)
        first_open = driver.ensure(self.first["id"])
        driver.ensure(self.second["id"])
        self.assertEqual(len(driver.managers), 2)
        first_manager = driver.managers[self.first["id"]]
        second_manager = driver.managers[self.second["id"]]
        self.assertIsNot(first_manager, second_manager)
        self.assertEqual(first_manager.config.agent_dir, bindings[self.first["id"]].spec.agent_dir)
        self.assertEqual(second_manager.config.session_dir, bindings[self.second["id"]].spec.session_dir)
        self.assertEqual(first_manager.config.tool_gateway_token, "grant-" + self.first["id"])
        self.assertEqual(first_manager.config.runtime_environment["HOME"], "/run/paw/home")
        self.assertEqual(first_manager.config.runtime_environment["RAG_IME_TEAM_WORKER"], "1")
        self.assertEqual(first_manager.config.provider_environment, {})
        requests = [
            json.loads(line)
            for line in (first_manager.config.agent_dir / "host-requests.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        opened = next(item["params"] for item in requests if item["method"] == "session.open")
        self.assertEqual(opened["cwd"], "/workspace")
        self.assertNotIn(str(bindings[self.first["id"]].spec.workspace_root), repr(opened))
        self.assertEqual(
            first_open["state"]["sessionFile"],
            f"/run/paw/sessions/{self.first['id']}.jsonl",
        )
        binding = self.store.runtime_binding(self.first["id"])
        self.assertEqual(
            binding["transcriptRef"],
            str(bindings[self.first["id"]].spec.session_dir / f"{self.first['id']}.jsonl"),
        )
        driver.stop()

    def _package_driver(self):
        binding = self._binding(self.first['id'])
        source = self.root / 'fixed-resource'
        source.mkdir()
        package = StagedTeamPackage('fixture', '1.0.0', 'a' * 64, source.resolve(),
                                    {'packageId': 'fixture', 'version': '1.0.0'},
                                    {'id': 'fixture', 'version': '1.0.0'}, 'rag-ime-plugin.json')
        binding = replace(binding, spec=replace(binding.spec, resource_roots=(package.path,)),
                          packages=(package,), package_activation_token='this-attempt-only', skill_refs=('selected-skill',))
        manager = Mock(spec=PiRuntimeHostManager)
        manager.ensure.return_value = {'sessionId': self.first['id']}
        builder = Mock(return_value=manager)
        driver = TeamRuntimeDriver(
            base_config=self.config,
            context=RuntimeDriverContext(sessions=self.store, events=self.events, tool_gateway_token='unused',
                                         skill_allowlist_provider=lambda _session: ['private-host-skill']),
            purpose='interactive', binding_resolver=lambda _session: binding, manager_builder=builder,
        )
        self.addCleanup(driver.stop)
        return driver, manager, builder

    def test_fixed_packages_prepare_once_before_the_task_opens(self) -> None:
        driver, manager, builder = self._package_driver()
        with patch('rag_ime.team.resource_runtime.install_task_packages') as install:
            install.side_effect = lambda *_args: self.assertEqual(manager.ensure.call_count, 0)
            driver.ensure(self.first['id'])
            driver.ensure(self.first['id'])
        install.assert_called_once()
        package = install.call_args.args[1][0]
        self.assertEqual(str(package.path), '/run/paw/resources/0')
        scoped = builder.call_args.kwargs['config']
        self.assertEqual(scoped.plugin_approval_token, 'this-attempt-only')
        self.assertEqual(scoped.runtime_environment['RAG_IME_PLUGIN_APPROVAL_TOKEN'], 'this-attempt-only')
        self.assertEqual(scoped.provider_environment, {})
        context = builder.call_args.kwargs['context']
        self.assertEqual(context.skill_allowlist_provider(self.first), ['selected-skill'])
        self.assertIsNone(context.candidate_skill_paths_provider)

    def test_failed_or_stopped_preparation_never_opens_or_caches_the_task(self) -> None:
        driver, manager, _builder = self._package_driver()
        with patch('rag_ime.team.resource_runtime.install_task_packages', side_effect=AgentRuntimeError('fixture failure')):
            with self.assertRaises(AgentRuntimeError):
                driver.ensure(self.first['id'])
        self.assertEqual(driver.managers, {})
        manager.ensure.assert_not_called()
        self.assertGreaterEqual(manager.retire.call_count, 1)
        with patch('rag_ime.team.resource_runtime.install_task_packages', side_effect=lambda *_args: driver.stop()):
            with self.assertRaisesRegex(AgentRuntimeError, 'stopped'):
                driver.ensure(self.first['id'])
        self.assertEqual(driver.managers, {})
        manager.ensure.assert_not_called()

    def _real_race_driver(self, *, packages=False, resolver_hook=None):
        binding = self._binding(self.first['id'])
        launcher = Mock(wraps=binding.launcher, spec=_LocalLauncher)
        binding = replace(binding, launcher=launcher)
        if packages:
            source = self.root / 'fixed-race-resource'
            source.mkdir()
            package = StagedTeamPackage('fixture', '1.0.0', 'b' * 64, source.resolve(),
                                        {'packageId': 'fixture', 'version': '1.0.0'},
                                        {'id': 'fixture', 'version': '1.0.0'}, 'rag-ime-plugin.json')
            binding = replace(binding, spec=replace(binding.spec, resource_roots=(package.path,)),
                              packages=(package,), package_activation_token='race-attempt-only')
        self.store.set_runtime_policy(self.first['id'], mode='coordinator', tool_profile_version='control-center-v1',
                                      allowed_tools=None, project_context_enabled=False,
                                      workspace_roots=[str(binding.spec.workspace_root)])

        def resolve(_session):
            if resolver_hook is not None:
                resolver_hook()
            return binding

        driver = PiRuntimeDriverFactory(self.config, execution_binding_resolver=resolve).create(
            RuntimeDriverContext(sessions=self.store, events=self.events, tool_gateway_token='unused'),
            purpose='interactive',
        )
        self.addCleanup(driver.stop)
        return driver, launcher

    def test_concurrent_opens_share_one_package_preparation_and_host(self) -> None:
        driver, launcher = self._real_race_driver(packages=True)
        entered, release = threading.Event(), threading.Event()

        def prepare(_manager, _packages):
            entered.set()
            if not release.wait(10):
                raise AssertionError('Preparation fixture was not released')

        with patch('rag_ime.team.resource_runtime.install_task_packages', side_effect=prepare) as install:
            with ThreadPoolExecutor(max_workers=2) as workers:
                first = workers.submit(driver.ensure, self.first['id'])
                try:
                    self.assertTrue(entered.wait(5))
                    second = workers.submit(driver.ensure, self.first['id'])
                    # Neither caller may open an unprepared task. This also
                    # gives the overlapping caller time to reach admission.
                    with self.assertRaises(FutureTimeoutError):
                        second.result(timeout=0.1)
                    self.assertEqual(launcher.start.call_count, 0)
                finally:
                    release.set()
                first_result = first.result(timeout=5)
                second_result = second.result(timeout=5)
        self.assertEqual(install.call_count, 1)
        self.assertEqual(launcher.start.call_count, 1)
        self.assertEqual(first_result['state']['sessionId'], self.first['id'])
        self.assertEqual(second_result['state']['sessionId'], self.first['id'])

    def test_one_tasks_preparation_does_not_block_another_tasks_host(self) -> None:
        bindings, launchers = {}, {}
        source = self.root / 'parallel-resource'
        source.mkdir()
        package = StagedTeamPackage('fixture', '1.0.0', 'c' * 64, source.resolve(),
                                    {'packageId': 'fixture', 'version': '1.0.0'},
                                    {'id': 'fixture', 'version': '1.0.0'}, 'rag-ime-plugin.json')
        for session in (self.first, self.second):
            binding = self._binding(session['id'])
            launcher = Mock(wraps=binding.launcher, spec=_LocalLauncher)
            launchers[session['id']] = launcher
            bindings[session['id']] = replace(
                binding, launcher=launcher, packages=(package,), package_activation_token='parallel-fixture',
                spec=replace(binding.spec, resource_roots=(package.path,)),
            )
            self.store.set_runtime_policy(
                session['id'], mode='coordinator', tool_profile_version='control-center-v1',
                allowed_tools=None, project_context_enabled=False,
                workspace_roots=[str(binding.spec.workspace_root)],
            )
        driver = PiRuntimeDriverFactory(
            self.config, execution_binding_resolver=lambda session: bindings[session['id']],
        ).create(
            RuntimeDriverContext(sessions=self.store, events=self.events, tool_gateway_token='unused'),
            purpose='interactive',
        )
        self.addCleanup(driver.stop)
        entered, release = threading.Event(), threading.Event()

        def prepare(manager, _packages):
            if manager.config.session_dir.resolve() == bindings[self.first['id']].spec.session_dir.resolve():
                entered.set()
                if not release.wait(10):
                    raise AssertionError('Preparation fixture was not released')

        with patch('rag_ime.team.resource_runtime.install_task_packages', side_effect=prepare):
            with ThreadPoolExecutor(max_workers=2) as workers:
                first = workers.submit(driver.ensure, self.first['id'])
                try:
                    self.assertTrue(entered.wait(5))
                    second = workers.submit(driver.ensure, self.second['id'])
                    second_result = second.result(timeout=5)
                    self.assertEqual(second_result['state']['sessionId'], self.second['id'])
                    self.assertEqual(launchers[self.first['id']].start.call_count, 0)
                    self.assertEqual(launchers[self.second['id']].start.call_count, 1)
                finally:
                    release.set()
                first.result(timeout=5)
        self.assertEqual(len(driver.managers), 2)

    def test_failed_package_host_is_removed_before_a_fresh_retry_succeeds(self) -> None:
        driver, launcher = self._real_race_driver(packages=True)

        def fail_after_start(manager, _packages):
            manager.plugin_list()
            raise AgentRuntimeError('synthetic Package preparation failure')

        with patch('rag_ime.team.resource_runtime.install_task_packages', side_effect=fail_after_start):
            with self.assertRaisesRegex(AgentRuntimeError, 'Package preparation failure'):
                driver.ensure(self.first['id'])
        self.assertEqual(driver.managers, {})
        self.assertEqual(launcher.start.call_count, 1)
        self.assertEqual(launcher.stop.call_count, 1)
        failed_process = launcher.stop.call_args.args[1]
        self.assertIsNotNone(failed_process.poll())
        with patch('rag_ime.team.resource_runtime.install_task_packages'):
            result = driver.ensure(self.first['id'])
        self.assertEqual(result['state']['sessionId'], self.first['id'])
        self.assertEqual(launcher.start.call_count, 2)
        self.assertEqual(len(driver.managers), 1)

    def test_stop_between_manager_acquisition_and_ensure_cannot_reopen_old_host(self) -> None:
        driver, launcher = self._real_race_driver()
        original = driver._manager_for

        def acquire_then_stop(session_id):
            manager = original(session_id)
            self.addCleanup(manager.stop)
            driver.stop()
            return manager

        with patch.object(driver, '_manager_for', side_effect=acquire_then_stop):
            with self.assertRaises(AgentRuntimeError):
                driver.ensure(self.first['id'])
        launcher.start.assert_not_called()
        self.assertEqual(driver.managers, {})
        # A separate, later request may obtain a fresh manager. The discarded
        # reference must never be the object that reopens it.
        driver.ensure(self.first['id'])
        self.assertEqual(launcher.start.call_count, 1)

    def test_stop_before_package_host_admission_rejects_preflight(self) -> None:
        driver, launcher = self._real_race_driver(packages=True)

        def preflight_after_stop(manager, packages):
            driver.stop()
            manager.plugin_validate(str(packages[0].path))

        with patch('rag_ime.team.resource_runtime.install_task_packages', side_effect=preflight_after_stop):
            with self.assertRaises(AgentRuntimeError):
                driver.ensure(self.first['id'])
        launcher.start.assert_not_called()
        self.assertEqual(driver.managers, {})

    def test_stop_during_binding_resolution_rejects_the_old_call(self) -> None:
        entered, resume = threading.Event(), threading.Event()

        def pause_resolution():
            entered.set()
            if not resume.wait(5):
                raise RuntimeError('fixture binding wait timed out')

        driver, launcher = self._real_race_driver(resolver_hook=pause_resolution)
        failures = []

        def ensure():
            try:
                driver.ensure(self.first['id'])
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=ensure, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            driver.stop()
        finally:
            resume.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], AgentRuntimeError)
        launcher.start.assert_not_called()
        self.assertEqual(driver.managers, {})

    def test_stop_closes_admission_until_retirement_finishes(self) -> None:
        driver, launcher = self._real_race_driver()
        manager = driver._manager_for(self.first['id'])
        entered, resume = threading.Event(), threading.Event()
        retire = manager.retire

        def pending_retirement():
            entered.set()
            if not resume.wait(5):
                raise RuntimeError('fixture retirement wait timed out')
            retire()

        with patch.object(manager, 'retire', side_effect=pending_retirement):
            worker = threading.Thread(target=driver.stop, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaisesRegex(AgentRuntimeError, 'stopping'):
                    driver.ensure(self.first['id'])
            finally:
                resume.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        launcher.start.assert_not_called()

    def test_ordinary_manager_stop_remains_restartable_until_retired(self) -> None:
        driver, launcher = self._real_race_driver()
        manager = driver._manager_for(self.first['id'])
        manager.ensure(self.first['id'])
        manager.stop()
        manager.ensure(self.first['id'])
        self.assertEqual(launcher.start.call_count, 2)
        manager.retire()
        with self.assertRaisesRegex(AgentRuntimeError, 'retired'):
            manager.ensure(self.first['id'])
        self.assertEqual(launcher.start.call_count, 2)

    def test_binding_rejects_provider_credentials_other_than_attempt_token(self) -> None:
        binding = self._binding(self.first["id"])
        unsafe = replace(
            binding,
            model_providers={
                "team": {
                    "baseUrl": "http://127.0.0.1:8766/v1",
                    "apiKey": "upstream-secret",
                }
            },
        )
        with self.assertRaisesRegex(ValueError, "credential"):
            unsafe.scoped_config(self.config)

        safe = replace(
            binding,
            model_providers={
                "team": {
                    "baseUrl": "http://127.0.0.1:8766/v1",
                    "apiKey": binding.gateway_token,
                }
            },
        )
        self.assertEqual(
            safe.scoped_config(self.config).model_providers["team"]["apiKey"],
            binding.gateway_token,
        )

    def test_isolated_environment_drops_host_certificate_paths(self) -> None:
        binding = self._binding(self.first["id"])
        config = binding.scoped_config(self.config)
        with patch.dict(
            "os.environ",
            {
                "SSL_CERT_FILE": "/host/private/cert.pem",
                "SSL_CERT_DIR": "/host/private/certs",
            },
            clear=False,
        ):
            child = config.child_environment(session=self.first)
        self.assertNotIn("SSL_CERT_FILE", child)
        self.assertNotIn("SSL_CERT_DIR", child)
        self.assertEqual(child["HOME"], "/run/paw/home")

    def test_transcript_path_mapping_stays_inside_the_attempt_session_mount(self) -> None:
        binding = self._binding(self.first["id"])
        config = binding.scoped_config(self.config)
        host_transcript = binding.spec.session_dir / "session-1.jsonl"
        container_transcript = "/run/paw/sessions/session-1.jsonl"
        self.assertEqual(config.container_session_path(str(host_transcript)), container_transcript)
        self.assertEqual(config.host_session_path(container_transcript), str(host_transcript))
        with self.assertRaisesRegex(PiRuntimeError, "outside"):
            config.container_session_path(str(self.root / "outside.jsonl"))
        with self.assertRaisesRegex(PiRuntimeError, "outside"):
            config.host_session_path("/run/paw/home/outside.jsonl")

    def test_host_transcript_snapshot_rejects_file_and_directory_symlinks(self) -> None:
        session_root = self.config.session_dir
        session_root.mkdir(parents=True)
        outside = self.root / "transcript-outside"
        outside.mkdir()
        header = json.dumps({"type": "session", "id": "pi:test"}) + "\n"
        real = session_root / "real.jsonl"
        real.write_text(header, encoding="utf-8")
        manager = PiRuntimeHostManager(
            config=self.config,
            sessions=self.store,
            events=self.events,
        )
        self.addCleanup(manager.stop)

        def bind(path: Path) -> None:
            self.store.bind_runtime_session(
                self.first["id"],
                driver_id="managed-pi",
                runtime_kind="pi_rpc",
                external_session_id="pi:test",
                transcript_ref=str(path),
                updated_at_ms=1,
            )

        file_link = session_root / "file-link.jsonl"
        file_link.symlink_to(real)
        bind(file_link)
        self.assertIsNone(manager._durable_history_snapshot(self.first["id"]))

        directory_link = session_root / "directory-link"
        directory_link.symlink_to(outside, target_is_directory=True)
        bind(directory_link / "history.jsonl")
        self.assertIsNone(manager._durable_history_snapshot(self.first["id"]))

    def test_team_driver_requires_a_binding_for_every_session(self) -> None:
        factory = PiRuntimeDriverFactory(
            self.config,
            execution_binding_resolver=lambda _session: None,
        )
        driver = factory.create(
            RuntimeDriverContext(sessions=self.store, events=self.events, tool_gateway_token="x"),
            purpose="interactive",
        )
        with self.assertRaisesRegex(RuntimeError, "binding"):
            driver.ensure(self.first["id"])

    def test_cold_readonly_snapshot_does_not_start_or_bind_a_worker(self) -> None:
        factory = PiRuntimeDriverFactory(
            self.config,
            execution_binding_resolver=lambda _session: self.fail(
                "a readonly snapshot must not resolve an execution binding"
            ),
        )
        driver = factory.create(
            RuntimeDriverContext(
                sessions=self.store,
                events=self.events,
                tool_gateway_token="x",
            ),
            purpose="interactive",
        )
        snapshot = driver.session_snapshot(self.first["id"])
        self.assertEqual(snapshot["snapshotScope"], "persisted")
        self.assertEqual(snapshot["messages"], [])
        self.assertEqual(driver.messages(self.first["id"]), [])
        self.assertEqual(driver.command_catalog(self.first["id"]), [])
        self.assertEqual(driver.fork_candidates(self.first["id"]), [])
        self.assertEqual(driver.tool_catalog(self.first["id"]), [])
        self.assertEqual(driver.model_catalog(self.first["id"])["runtimeResident"], False)
        self.assertEqual(driver.available_models()[0]["id"], "test-model")
        self.assertEqual(driver.plugin_list(), [])
        self.assertEqual(driver.plugin_catalog(), [])
        self.assertEqual(driver.pending_ui_requests(self.first["id"]), [])
        self.assertIsNone(driver.abort(self.first["id"]))
        self.assertFalse(driver.close_session(self.first["id"]))
        self.assertFalse(driver.has_pending_approval(self.first["id"], "approval-1"))
        self.assertFalse(driver.has_pending_review(self.first["id"], "review-1"))
        self.assertEqual(driver.managers, {})

    def test_cold_snapshot_does_not_claim_persisted_active_status_is_running(self) -> None:
        self.store.set_status(self.first["id"], "active")
        driver = PiRuntimeDriverFactory(
            self.config,
            execution_binding_resolver=lambda _session: self.fail(
                "a cold status read must not resolve an execution binding"
            ),
        ).create(
            RuntimeDriverContext(
                sessions=self.store,
                events=self.events,
                tool_gateway_token="unused-process-token",
            ),
            purpose="interactive",
        )
        full = driver.session_snapshot(self.first["id"])
        recent = driver.recent_session_snapshot(self.first["id"])
        self.assertTrue(full["isIdle"])
        self.assertTrue(recent["isIdle"])
        self.assertEqual(full["historyState"], "missing")
        self.assertEqual(recent["historyState"], "missing")
        self.assertEqual(driver.managers, {})
        driver.stop()

    def test_cold_available_models_is_persisted_without_grant_or_worker(self) -> None:
        resolver_calls: list[str] = []

        def resolver(session: object) -> object:
            resolver_calls.append(str(session))
            self.fail("cold model discovery must not resolve an execution binding")

        driver = PiRuntimeDriverFactory(
            self.config,
            execution_binding_resolver=resolver,
        ).create(
            RuntimeDriverContext(
                sessions=self.store,
                events=self.events,
                tool_gateway_token="unused-process-token",
            ),
            purpose="interactive",
        )
        self.assertEqual(
            driver.available_models(),
            [{"provider": "test", "id": "test-model", "name": "test-model"}],
        )
        self.assertEqual(resolver_calls, [])
        self.assertEqual(driver.managers, {})

    def test_restarted_team_driver_reads_stable_transcript_without_starting_pi(self) -> None:
        history_root = self.root / "stable-history"
        history_root.mkdir()
        session_id = str(self.first["id"])
        transcript = history_root / f"{session_id}.jsonl"
        entries = [
            {"type": "session", "id": "pi-restarted"},
            {
                "type": "message",
                "id": "restart-user",
                "parentId": "",
                "timestamp": 100,
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "重启前的问题"}],
                },
            },
            {
                "type": "message",
                "id": "restart-assistant",
                "parentId": "restart-user",
                "timestamp": 101,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "重启后的回答"}],
                },
            },
        ]
        transcript.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-restarted",
            transcript_ref=str(transcript),
            branch_anchor="restart-assistant",
            binding_state="active",
            metadata={"protocolVersion": "2"},
            message_count=2,
        )
        resolver_calls: list[str] = []
        start_calls: list[str] = []

        def resolver(session: Mapping[str, object]) -> object:
            resolver_calls.append(str(session.get("id")))
            self.fail("restarted readonly projection must not resolve a binding")

        with patch(
            "rag_ime.pi.host_client.PiRuntimeHostClient.start",
            side_effect=lambda self: start_calls.append(self.host_identity),
        ):
            driver = PiRuntimeDriverFactory(
                self.config,
                execution_binding_resolver=resolver,
                execution_history_root=lambda _session_id: history_root,
            ).create(
                RuntimeDriverContext(
                    sessions=self.store,
                    events=self.events,
                    tool_gateway_token="unused-process-token",
                ),
                purpose="interactive",
            )
            full = driver.session_snapshot(session_id)
            recent = driver.recent_session_snapshot(session_id)

        full_text = [
            block["data"]["text"]
            for message in full["messages"]
            for block in message["blocks"]
            if block["type"] == "text"
        ]
        recent_text = [
            block["data"]["text"]
            for message in recent["messages"]
            for block in message["blocks"]
            if block["type"] == "text"
        ]
        self.assertEqual(full_text, ["重启前的问题", "重启后的回答"])
        self.assertEqual(recent_text, ["重启前的问题", "重启后的回答"])
        self.assertEqual(resolver_calls, [])
        self.assertEqual(start_calls, [])
        self.assertEqual(driver.managers, {})
        driver.stop()

    def test_cold_history_rejects_transcript_outside_injected_root(self) -> None:
        history_root = self.root / "stable-history"
        outside = self.root / "other-history"
        history_root.mkdir()
        outside.mkdir()
        transcript = outside / f"{self.first['id']}.jsonl"
        transcript.write_text(
            json.dumps({"type": "session", "id": "outside"}) + "\n",
            encoding="utf-8",
        )
        self.store.bind_runtime_session(
            self.first["id"],
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="outside",
            transcript_ref=str(transcript),
            binding_state="active",
            metadata={"protocolVersion": "2"},
        )
        resolver_calls: list[str] = []
        driver = PiRuntimeDriverFactory(
            self.config,
            execution_binding_resolver=lambda session: resolver_calls.append(
                str(session.get("id"))
            ) or self.fail("cross-root cold history must not resolve a binding"),
            execution_history_root=lambda _session_id: history_root,
        ).create(
            RuntimeDriverContext(
                sessions=self.store,
                events=self.events,
                tool_gateway_token="unused-process-token",
            ),
            purpose="interactive",
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "outside its scoped history root",
        ):
            driver.session_snapshot(self.first["id"])
        with self.assertRaisesRegex(
            RuntimeError,
            "outside its scoped history root",
        ):
            driver.recent_session_snapshot(self.first["id"])
        self.assertEqual(resolver_calls, [])
        self.assertEqual(driver.managers, {})
        driver.stop()

    def test_cold_history_rejects_symlinked_injected_root(self) -> None:
        history_target = self.root / "stable-history-target"
        history_target.mkdir()
        history_link = self.root / "stable-history-link"
        history_link.symlink_to(history_target, target_is_directory=True)
        driver = PiRuntimeDriverFactory(
            self.config,
            execution_binding_resolver=lambda _session: self.fail(
                "unsafe history root must not resolve an execution binding"
            ),
            execution_history_root=lambda _session_id: history_link,
        ).create(
            RuntimeDriverContext(
                sessions=self.store,
                events=self.events,
                tool_gateway_token="unused-process-token",
            ),
            purpose="interactive",
        )
        with self.assertRaisesRegex(RuntimeError, "history root must not be a symlink"):
            driver.session_snapshot(self.first["id"])
        with self.assertRaisesRegex(RuntimeError, "history root must not be a symlink"):
            driver.recent_session_snapshot(self.first["id"])
        self.assertEqual(driver.managers, {})
        driver.stop()

    def test_cold_history_exposes_existing_corrupt_transcript_as_unavailable(self) -> None:
        history_root = self.root / "stable-history"
        history_root.mkdir()
        transcript = history_root / f"{self.first['id']}.jsonl"
        transcript.write_text("{this is not valid JSON}\n", encoding="utf-8")
        self.store.bind_runtime_session(
            self.first["id"],
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-corrupt",
            transcript_ref=str(transcript),
            binding_state="active",
            metadata={"protocolVersion": "2"},
        )
        resolver_calls: list[str] = []
        driver = PiRuntimeDriverFactory(
            self.config,
            execution_binding_resolver=lambda session: resolver_calls.append(
                str(session.get("id"))
            ) or self.fail("corrupt cold history must not resolve a binding"),
            execution_history_root=lambda _session_id: history_root,
        ).create(
            RuntimeDriverContext(
                sessions=self.store,
                events=self.events,
                tool_gateway_token="unused-process-token",
            ),
            purpose="interactive",
        )
        with self.assertRaisesRegex(RuntimeError, "transcript is malformed"):
            driver.session_snapshot(self.first["id"])
        with self.assertRaisesRegex(RuntimeError, "transcript is malformed"):
            driver.recent_session_snapshot(self.first["id"])
        self.assertEqual(resolver_calls, [])
        self.assertEqual(driver.managers, {})
        driver.stop()

    def test_cold_abort_stops_execution_without_resolving_a_binding(self) -> None:
        resolver_calls: list[str] = []
        stop_calls: list[str] = []

        def resolver(session: object) -> object:
            resolver_calls.append(str(session))
            self.fail("cold abort must not resolve an execution binding")

        driver = PiRuntimeDriverFactory(
            self.config,
            execution_binding_resolver=resolver,
            execution_stop=stop_calls.append,
        ).create(
            RuntimeDriverContext(
                sessions=self.store,
                events=self.events,
                tool_gateway_token="unused-process-token",
            ),
            purpose="interactive",
        )
        self.assertIsNone(driver.abort(self.first["id"]))
        self.assertEqual(stop_calls, [self.first["id"]])
        self.assertEqual(resolver_calls, [])
        self.assertEqual(driver.managers, {})

    def test_hot_abort_runs_pi_abort_before_execution_stop(self) -> None:
        binding = self._binding(self.first["id"])
        self.store.set_runtime_policy(
            self.first["id"],
            mode="coordinator",
            tool_profile_version="control-center-v1",
            allowed_tools=None,
            project_context_enabled=False,
            workspace_roots=[str(binding.spec.workspace_root)],
        )
        stop_calls: list[str] = []
        driver = PiRuntimeDriverFactory(
            self.config,
            execution_binding_resolver=lambda _session: binding,
            execution_stop=stop_calls.append,
        ).create(
            RuntimeDriverContext(
                sessions=self.store,
                events=self.events,
                tool_gateway_token="unused-process-token",
            ),
            purpose="interactive",
        )
        driver.ensure(self.first["id"])
        result = driver.abort(self.first["id"])
        self.assertIsInstance(result, dict)
        self.assertEqual(stop_calls, [self.first["id"]])
        driver.stop()

    def test_execution_stop_failure_is_propagated_from_cold_abort(self) -> None:
        def stop(_session_id: str) -> None:
            raise RuntimeError("container stop could not be verified")

        driver = PiRuntimeDriverFactory(
            self.config,
            execution_binding_resolver=lambda _session: self.fail(
                "failed cold abort must not resolve a binding"
            ),
            execution_stop=stop,
        ).create(
            RuntimeDriverContext(
                sessions=self.store,
                events=self.events,
                tool_gateway_token="unused-process-token",
            ),
            purpose="interactive",
        )
        with self.assertRaisesRegex(RuntimeError, "could not be verified"):
            driver.abort(self.first["id"])
        self.assertEqual(driver.managers, {})

    def test_isolated_host_command_can_live_only_inside_runtime_image(self) -> None:
        binding = self._binding(self.first["id"])
        self.store.set_runtime_policy(
            self.first["id"],
            mode="coordinator",
            tool_profile_version="control-center-v1",
            allowed_tools=None,
            project_context_enabled=False,
            workspace_roots=[str(binding.spec.workspace_root)],
        )
        config = replace(self.config, executable=None, installation_error="")
        factory = PiRuntimeDriverFactory(
            config,
            execution_binding_resolver=lambda _session: binding,
        )
        driver = factory.create(
            RuntimeDriverContext(
                sessions=self.store,
                events=self.events,
                tool_gateway_token="x",
            ),
            purpose="interactive",
        )
        opened = driver.ensure(self.first["id"])
        self.assertEqual(opened["state"]["sessionId"], self.first["id"])
        self.assertEqual(driver.runtime_status()["status"], "ready")
        driver.stop()


class _LocalLauncher:
    """Test-only process adapter; production team bindings use Docker/OCI."""

    def start(self, spec, command, environment):
        spec.validate_for_launch()
        # The local fake runs outside an OCI mount namespace. Mirror the
        # container paths back to the temporary host scope so its fixture can
        # record requests while exercising the real Pi path translation.
        environment = dict(environment)
        environment.update(
            {
                "HOME": str(spec.home_dir),
                "TMPDIR": str(spec.tmp_dir),
                "RAG_IME_PI_AGENT_DIR": str(spec.agent_dir),
                "RAG_IME_PI_SESSION_DIR": str(spec.session_dir),
            }
        )
        return subprocess.Popen(
            # Run the Python fixture with the test runner's interpreter.
            # /usr/bin/env python3 may select a different installation and
            # stall before the fixture reads its first protocol request.
            [sys.executable, *command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=spec.agent_dir,
            env=dict(environment),
            bufsize=0,
            start_new_session=True,
        )

    def stop(self, spec, process):
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)
        return ExecutionReceipt(
            attempt_id=spec.attempt_id,
            session_id=spec.session_id,
            container_id=spec.container_id,
            action="stop",
            state="stopped",
            verified=True,
        )


if __name__ == "__main__":
    unittest.main()
