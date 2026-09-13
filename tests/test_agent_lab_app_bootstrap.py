from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_lab.app_sources import export_zip, freeze_source
from tests.test_agent_lab_apps import MODEL, write_app


class AppBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write_app(self.root)

    def package(self):
        version = {**freeze_source(self.root, 'app', MODEL), 'appId': 'test-app', 'version': 1}
        return export_zip(version, 'standalone')[1]

    def test_export_has_frozen_launcher_and_explicit_setup_entry(self):
        archive = self.package()
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            self.assertTrue({'launch.py', 'Start.command', 'requirements-app.txt'} <= set(bundle.namelist()))
            compile(bundle.read('launch.py'), 'launch.py', 'exec')
            self.assertIn('launch.py --setup', bundle.read('README.md').decode())
            self.assertEqual(bundle.getinfo('Start.command').external_attr >> 16 & 0o777, 0o755)
            self.assertNotIn(str(self.root), bundle.read('launch.py').decode())

    def test_dense_archive_pins_dependencies_and_model_in_first_run_instructions(self):
        source = self.root / 'app' / 'app.json'
        spec = json.loads(source.read_text()); spec['knowledge'] = {'indexId': 'fixture'}
        source.write_text(json.dumps(spec))
        config = {'profile': {'mode': 'dense', 'topK': 2, 'contextChars': 4000}, 'queryField': 'question',
                  'documentCount': 2, 'chunkCount': 3, 'sourceCount': 2,
                  'embedding': {'provider': 'sentence-transformers', 'model': 'example/model', 'modelRevision': 'a' * 40}}
        version = {**freeze_source(self.root, 'app', MODEL, freeze_knowledge=lambda _: {'knowledge': config, 'files': {}}),
                   'appId': 'fixture', 'version': 1}
        with zipfile.ZipFile(io.BytesIO(export_zip(version, 'standalone')[1])) as bundle:
            requirements = bundle.read('requirements-app.txt').decode()
            self.assertIn('torch==2.13.0', requirements)
            self.assertIn('sentence-transformers==5.6.0', requirements)
            self.assertIn('transformers==5.13.1', requirements)
            self.assertIn('huggingface-hub==1.23.0', requirements)
            self.assertIn('a' * 40, bundle.read('README.md').decode())
            self.assertIn('HF_HUB_CACHE', bundle.read('README.md').decode())

    def test_public_version_lists_launcher_hashes_without_shipping_raw_source(self):
        import hashlib
        from rag_ime.agent_lab.apps import AgentLabAppStore
        version = {**freeze_source(self.root, 'app', MODEL), 'appId': 'test-app', 'version': 1}
        public = AgentLabAppStore._public_version(version)
        self.assertNotIn('bootstrapFiles', public)
        files = {row['path']: row for row in public['sourceFiles']}
        for name, body in version['bootstrapFiles'].items():
            self.assertEqual(files[name]['sha256'], hashlib.sha256(body.encode()).hexdigest())
            self.assertEqual(files[name]['byteSize'], len(body.encode()))
        self.assertEqual(public['fileCount'], len(files))
        self.assertEqual(public['byteSize'], sum(row['byteSize'] for row in files.values()))

    def test_launcher_is_frozen_with_version_not_later_product_code(self):
        version = {**freeze_source(self.root, 'app', MODEL), 'appId': 'test-app', 'version': 1}
        before = export_zip(version, 'standalone')
        original = Path.read_text
        def changed(path, *args, **kwargs):
            return '# changed' if path.name == 'app_bootstrap.py' else original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', changed):
            self.assertEqual(export_zip(version, 'standalone'), before)

    def test_launcher_checks_and_starts_real_isolated_export_without_install(self):
        target = self.root / 'export'; target.mkdir()
        with zipfile.ZipFile(io.BytesIO(self.package())) as bundle:
            bundle.extractall(target)
        command = [sys.executable, str(target / 'launch.py'), '--python', sys.executable]
        env = {key: value for key, value in os.environ.items() if key not in {'APP_PAW_GATEWAY_URL', 'APP_API_KEY', 'APP_API_BASE_URL'}}
        checked = subprocess.run([*command, '--check'], capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertFalse((target / '.venv').exists())
        # Actual CLI forwarding to the frozen runner, without starting a model.
        with subprocess.Popen([*command, '--port', '0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env) as process:
            try:
                import select
                self.assertTrue(select.select([process.stdout], [], [], 10)[0], 'runner never announced URL')
                line = process.stdout.readline()
                url = line.strip().split()[-1]
                from urllib.request import urlopen
                with urlopen(url + '/health', timeout=3) as response:
                    health = json.load(response)
                self.assertEqual(health['runtime'], 'standalone')
                self.assertFalse(health['configured'])
            finally:
                process.terminate(); process.wait(timeout=5)
        self.assertFalse((target / '.venv').exists())

    def test_missing_environment_exits_with_setup_instruction_and_no_install(self):
        target = self.root / 'missing'; target.mkdir()
        with zipfile.ZipFile(io.BytesIO(self.package())) as bundle:
            bundle.extractall(target)
        result = subprocess.run([sys.executable, str(target / 'launch.py'), '--check'], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--setup', result.stdout + result.stderr)
        self.assertFalse((target / '.venv').exists())


class BootstrapSetupTests(unittest.TestCase):
    def setUp(self):
        from rag_ime.agent_lab import app_bootstrap
        self.bootstrap = app_bootstrap
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'app.json').write_text(json.dumps({'knowledge': {'profile': {'mode': 'hybrid'},
            'embedding': {'provider': 'sentence-transformers', 'model': 'example/model', 'modelRevision': 'b' * 40}}}))

    def test_invalid_or_machine_specific_model_never_installs(self):
        spec = json.loads((self.root / 'app.json').read_text())
        spec['knowledge']['embedding']['model'] = '/private/model-cache'
        (self.root / 'app.json').write_text(json.dumps(spec))
        with patch.object(self.bootstrap.subprocess, 'run') as run:
            self.assertEqual(self.bootstrap.main(['--setup'], root=self.root), 1)
        run.assert_not_called()

    def test_explicit_paw_launch_reuses_current_interpreter_without_install_or_dense_probe(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(self.bootstrap.os, 'execve') as launch, \
                patch.object(self.bootstrap, '_run_probe') as probe, patch.object(self.bootstrap, 'setup') as install:
            self.assertEqual(self.bootstrap.main(['--paw', '--port', '8083'], root=self.root), 0)
        probe.assert_not_called()
        install.assert_not_called()
        self.assertEqual(launch.call_args.args[0], str(Path(sys.executable).absolute()))
        self.assertEqual(launch.call_args.args[2]['APP_PAW_GATEWAY_URL'], 'http://127.0.0.1:8768')
        self.assertIn('8083', launch.call_args.args[1])
        self.assertFalse((self.root / '.venv').exists())

    def test_encoder_setup_uses_exact_commit_no_token_and_offline_check(self):
        import types
        fake = types.ModuleType('sentence_transformers'); fake.SentenceTransformer = unittest.mock.Mock()
        config = self.bootstrap.embedding_config(self.root)
        with patch.dict(sys.modules, {'sentence_transformers': fake}), patch.dict(os.environ, {'HF_HUB_CACHE': str(self.root / 'cache')}):
            self.bootstrap.prepare_encoder(config, download=False)
            self.assertEqual(fake.SentenceTransformer.call_args.kwargs['revision'], 'b' * 40)
            self.assertTrue(fake.SentenceTransformer.call_args.kwargs['local_files_only'])
            self.assertFalse(fake.SentenceTransformer.call_args.kwargs['token'])
            self.assertFalse(fake.SentenceTransformer.call_args.kwargs['trust_remote_code'])
            self.bootstrap.prepare_encoder(config, download=True)
            self.assertFalse(fake.SentenceTransformer.call_args.kwargs['local_files_only'])
            fake.SentenceTransformer.side_effect = RuntimeError('secret-url-token')
            with self.assertRaises(self.bootstrap.SetupError) as error:
                self.bootstrap.prepare_encoder(config, download=False)
            self.assertNotIn('secret-url-token', str(error.exception))
            self.assertIn('--setup', str(error.exception))

    def test_repeated_setup_reuses_complete_environment_and_does_not_download(self):
        python = self.root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        python.parent.mkdir(parents=True); python.touch()
        ready = subprocess.CompletedProcess([], 0, 'ready', '')
        with patch.object(self.bootstrap, '_run_probe', return_value=ready) as probe, patch.object(self.bootstrap.subprocess, 'run') as run:
            self.bootstrap.setup(self.root, {})
            self.bootstrap.setup(self.root, {})
        run.assert_not_called()
        self.assertEqual(probe.call_count, 4)
        self.assertTrue(all('--_download' not in call.args for call in probe.call_args_list))

    def test_partial_setup_repairs_dependencies_and_model_then_retains_environment(self):
        python = self.root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        python.parent.mkdir(parents=True); python.touch()
        failed = subprocess.CompletedProcess([], 1, 'missing', '')
        ready = subprocess.CompletedProcess([], 0, 'ready', '')
        with patch.object(self.bootstrap, '_run_probe', side_effect=[failed, failed, ready]) as probe, patch.object(self.bootstrap.subprocess, 'run', return_value=ready) as install:
            self.bootstrap.setup(self.root, {})
        self.assertIn('pip', install.call_args.args[0]); self.assertIn('--_download', probe.call_args.args)
        with patch.object(self.bootstrap, '_run_probe', return_value=failed), patch.object(self.bootstrap.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'secret-token')):
            with self.assertRaises(self.bootstrap.SetupError) as error:
                self.bootstrap.setup(self.root, {})
        self.assertNotIn('secret-token', str(error.exception)); self.assertIn('--setup', str(error.exception))
        self.assertTrue(python.exists())
