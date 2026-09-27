from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from rag_ime.browser_control import BrowserControlService

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ego_runtime_build", ROOT / "scripts/build_ego_browser_runtime.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for runtime import validation")
class EgoBrowserRuntimeBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="paw browser runtime with spaces ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.output = self.root / "installed/app/integrations/ego-browser/upstream"
        self.node = shutil.which("node")
        for name in builder.REQUIRED:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"type":"module"}' if path.name == "package.json" else "export {};\n")
        (self.source / "package/ego-linux-host/bin/ego-browser.mjs").write_text('import "../dist/cli.js";\n')
        self.extra = self.source / "package/ego-linux-host/dist/dependency.js"
        self.extra.write_text("export {};\n")
        (self.source / "package/ego-linux-host/dist/cli.js").write_text('import "./dependency.js";\n')
        (self.source / "package/ego-linux-host/dist/unused.test.js").write_text("throw new Error('test must not ship');")
        (self.source / "package/ego-linux-host/node_modules").mkdir()

    def test_staged_payload_is_self_contained_and_matches_real_consumer_paths(self):
        result = builder.stage_payload(self.source, self.output, node=self.node)
        service = BrowserControlService(self.root / "isolated.sqlite", app_support_root=self.root / "support",
                                        ego_runtime_root=self.output)
        self.assertTrue(service._ego_runtime_available())
        self.assertFalse(list(self.output.rglob("node_modules")))
        self.assertFalse(list(self.output.rglob("*.test.js")))
        manifest = json.loads((self.output / builder.MARKER).read_text())
        self.assertEqual(manifest["treeSha256"], result["treeSha256"])
        self.assertIn("package/ego-linux-host/dist/dependency.js", manifest["files"])
        repeated = builder.stage_payload(self.source, self.root / "second", node=self.node)
        self.assertEqual(repeated["treeSha256"], result["treeSha256"])

    def test_incomplete_import_graph_does_not_replace_installed_runtime(self):
        builder.stage_payload(self.source, self.output, node=self.node)
        before = (self.output / builder.MARKER).read_bytes()
        self.extra.unlink()
        with self.assertRaises(subprocess.CalledProcessError):
            builder.stage_payload(self.source, self.output, node=self.node, replace=True)
        self.assertEqual((self.output / builder.MARKER).read_bytes(), before)

    def test_missing_harness_fails_before_publishing_output(self):
        (self.source / "package/ego-browser/dist/src/run.js").unlink()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            builder.stage_payload(self.source, self.output, node=self.node)
        self.assertFalse(self.output.exists())

    def test_existing_destination_requires_explicit_replace(self):
        self.output.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "exists"):
            builder.stage_payload(self.source, self.output, node=self.node)

    def test_official_source_and_binary_installers_include_the_same_runtime(self):
        sidecar = (ROOT / "scripts/install_sidecar_launch_agent.sh").read_text()
        packager = (ROOT / "scripts/build_macos_installer.py").read_text()
        self.assertIn('build_ego_browser_runtime.py" "${ego_args[@]}"', sidecar)
        self.assertIn('--from-built --node "$PAW_BINARY_PAYLOAD/pi-runtime/bin/node"', sidecar)
        self.assertIn("source / 'integrations/ego-browser/upstream'", packager)
        self.assertIn("'scripts/build_ego_browser_runtime.py',", packager)


if __name__ == "__main__":
    unittest.main()
