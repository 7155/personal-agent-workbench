from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.build_managed_pi_runtime_v2 import _prepare_sdk_prompt_overlay, _SDK_PROMPT_OVERLAYS, _SDK_PROMPT_OVERLAYS_V087


class PiPromptSettingsOverlayTests(unittest.TestCase):
    def test_native_summary_entry_preserves_lifecycle_arguments_and_combines_instructions(self) -> None:
        self._check_summary_entry(_SDK_PROMPT_OVERLAYS)

    def test_v087_summary_entry_preserves_lifecycle_arguments_and_combines_instructions(self) -> None:
        self._check_summary_entry(_SDK_PROMPT_OVERLAYS_V087)

    def _check_summary_entry(self, overlays) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is unavailable")
        with tempfile.TemporaryDirectory(prefix="paw-sdk-prompt-overlay-") as temporary:
            root = Path(temporary)
            sdk = root / "pi" / "packages" / "coding-agent"
            (root / "pi" / "node_modules").mkdir(parents=True)
            sdk.mkdir(parents=True)
            (sdk / "package.json").write_text('{"type":"module"}')
            for name, replacements in overlays.items():
                target = sdk / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("\n".join(before for before, _ in replacements))
            overlay = _prepare_sdk_prompt_overlay(root / "pi", root / "overlay")
            body = (overlay / "dist/core/agent-session.js").read_text()
            script = root / "probe.mjs"
            # Execute the patched shared native entry, with only the model call replaced.
            script.write_text("""const calls = [];
const compact = (...args) => { calls.push(args); return Promise.resolve({summary: 'fixture'}); };
class Session {
  settingsManager = { getCompactionSettings: () => ({instructions: '保留文档引用'}), getRetrySettings: () => ({maxRetries: 2}) };
  thinkingLevel = 'high'; agent = {streamFunction: 'original-stream'};
  _summarizationRetryCallbacks(value) { return value; }
""" + body + """\n}
const session = new Session();
await session._runDefaultCompaction('preparation', 'model', 'key', 'headers', undefined, 'signal', 'environment', 'threshold');
await session._runDefaultCompaction('preparation', 'model', 'key', 'headers', '本次保留测试命令', 'signal', 'environment', 'manual');
process.stdout.write(JSON.stringify(calls));
""")
            result = subprocess.run([node, str(script)], check=True, capture_output=True, text=True)
            automatic, manual = json.loads(result.stdout)
            self.assertEqual(automatic[4], "保留文档引用")
            self.assertEqual(manual[4], "保留文档引用\n\n本次保留测试命令")
            self.assertEqual(automatic[:4], manual[:4])
            self.assertEqual(automatic[5:10], manual[5:10])
            self.assertEqual(automatic[10], {"source": "compaction", "reason": "threshold"})
            self.assertEqual(manual[10], {"source": "compaction", "reason": "manual"})
            self.assertEqual((sdk / "dist/core/agent-session.js").read_text(), _SDK_PROMPT_OVERLAYS["dist/core/agent-session.js"][0][0])


if __name__ == "__main__":
    unittest.main()
