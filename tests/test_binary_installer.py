from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/install_binary_payload.py'
spec = importlib.util.spec_from_file_location('binary_installer', SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class BinaryPayloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='PAW payload with spaces ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in (
            'python/bin/python3', 'source/scripts/install_product_stack.sh',
            'apps/RagImeControlElectron.app/Contents/MacOS/RagImeControl',
            'apps/RagImeVoice.app/Contents/MacOS/RagImeVoice',
            'apps/RagImeDesktopBridge.app/Contents/MacOS/RagImeDesktopBridge',
            'pi-runtime/manifest.json',
            'source/integrations/ego-browser/upstream/paw-ego-browser-runtime.json',
            'source/integrations/ego-browser/upstream/package/ego-browser/dist/src/run.js',
            'source/integrations/ego-browser/upstream/package/ego-linux-host/dist/cli.js',
        ):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('component')
        (self.root / 'installer-manifest.json').write_text(json.dumps({
            'schemaVersion': 'paw.binary-installer.v1',
            'productSourceCommit': 'a' * 40,
            'files': installer.inventory(self.root),
        }))

    def test_corruption_stops_before_any_native_action(self):
        (self.root / 'python/bin/python3').write_text('corrupt')
        with patch.object(installer.subprocess, 'run') as native:
            with self.assertRaisesRegex(ValueError, 'payload content differs'):
                installer.verify(self.root)
            native.assert_not_called()

    def test_extra_module_cannot_enter_verified_runtime(self):
        (self.root / 'python/sitecustomize.py').write_text('untracked injection')
        with self.assertRaisesRegex(ValueError, 'unexpected payload content'):
            installer.verify(self.root)

    def test_external_symlink_is_rejected(self):
        (self.root / 'external').symlink_to('/etc')
        with self.assertRaisesRegex(ValueError, 'symlink escapes'):
            installer.inventory(self.root)

    def test_missing_component_fails_even_if_inventory_is_rewritten(self):
        (self.root / 'pi-runtime/manifest.json').unlink()
        manifest = json.loads((self.root / 'installer-manifest.json').read_text())
        manifest['files'] = installer.inventory(self.root)
        (self.root / 'installer-manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'missing required component'):
            installer.verify(self.root)

    def test_bundled_relative_links_and_receipts_remain_verifiable(self):
        executable = self.root / 'python/bin/python3'
        executable.rename(executable.with_name('python3.14'))
        executable.symlink_to('python3.14')
        manifest = json.loads((self.root / 'installer-manifest.json').read_text())
        manifest['files'] = installer.inventory(self.root)
        (self.root / 'installer-manifest.json').write_text(json.dumps(manifest))
        (self.root / 'pi-runtime.acceptance.json').write_text('{}')
        with patch.object(installer.subprocess, 'run') as native:
            installer.verify(self.root)
            if installer.sys.platform == 'darwin':
                self.assertEqual(native.call_count, 3)

    def test_missing_browser_runtime_is_not_a_complete_binary_payload(self):
        (self.root / 'source/integrations/ego-browser/upstream/package/ego-browser/dist/src/run.js').unlink()
        manifest = json.loads((self.root / 'installer-manifest.json').read_text())
        manifest['files'] = installer.inventory(self.root)
        (self.root / 'installer-manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'missing required component.*ego-browser'):
            installer.verify(self.root)


if __name__ == '__main__':
    unittest.main()
