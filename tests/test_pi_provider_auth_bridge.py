from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ESBUILD = ROOT.parent / "pi" / "node_modules" / ".bin" / "esbuild"
NODE = shutil.which("node")


@unittest.skipUnless(NODE and ESBUILD.is_file(), "local Pi build tools are not available")
class PiProviderAuthBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rag-ime-pi-oauth-bridge-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.auth_path = self.root / "auth.json"
        self.auth_path.write_text('{"existing":"preserve"}', encoding="utf-8")
        fixture = self.root / "oauth-fixture.mjs"
        fixture.write_text(
            """
import { writeFile } from 'node:fs/promises';

export class AuthStorage {
  static create(path) {
    return { modify: async (provider, update) => {
      const credential = await update();
      await writeFile(path, JSON.stringify({ provider, type: credential.type }));
    } };
  }
}
export class ModelRuntime {}
export const openaiCodexOAuth = {
  async login(interaction) {
    // Installed Pi requires this signal before device polling or browser waiting.
    if (interaction.signal.aborted) throw new Error('OAuth login cancelled');
    if (!(interaction.signal instanceof AbortSignal)) throw new Error('Missing AbortSignal');
    const method = await interaction.prompt({
      type: 'select', options: [{ id: 'browser' }, { id: 'device_code' }],
    });
    if (method !== 'device_code') throw new Error('Device login was not selected');
    interaction.notify({ type: 'device_code', userCode: 'TEST-CODE' });
    if (process.env.FIXTURE_OAUTH_FAILURE === '1') throw new Error('Fixture authorization failed');
    return { type: 'oauth', access: 'credential-sentinel-do-not-echo' };
  },
};
""",
            encoding="utf-8",
        )
        self.bridge = self.root / "provider-bridge.mjs"
        subprocess.run(
            [
                str(ESBUILD),
                str(ROOT / "rag_ime" / "node" / "pi_provider_bridge_bundled.ts"),
                "--bundle", "--platform=node", "--format=esm", "--target=node22",
                f"--outfile={self.bridge}",
                f"--alias:rag-ime-pi-auth-storage={fixture}",
                f"--alias:rag-ime-pi-model-runtime={fixture}",
                f"--alias:rag-ime-pi-openai-codex-oauth={fixture}",
                f"--alias:rag-ime-pi-openai-chatgpt-oauth={fixture}",
            ],
            check=True, capture_output=True, text=True, timeout=20,
        )

    def run_device_login(self, *, fail: bool = False) -> subprocess.CompletedProcess[str]:
        environment = {
            key: os.environ[key]
            for key in ("PATH", "LANG", "TMPDIR")
            if key in os.environ
        }
        environment["FIXTURE_OAUTH_FAILURE"] = "1" if fail else "0"
        return subprocess.run(
            [str(NODE), str(self.bridge)],
            input=json.dumps({
                "action": "oauth_device_code",
                "provider": "openai-codex",
                "agentDir": str(self.root),
            }),
            capture_output=True, text=True, timeout=5, env=environment,
        )

    def test_device_code_login_passes_the_required_abort_signal(self) -> None:
        completed = self.run_device_login()
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        events = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([event["event"] for event in events], ["state", "device_code", "completed"])
        self.assertEqual(json.loads(self.auth_path.read_text()), {"provider": "openai-codex", "type": "oauth"})
        self.assertNotIn("credential-sentinel-do-not-echo", completed.stdout + completed.stderr)

    def test_failed_device_login_preserves_the_existing_credentials(self) -> None:
        before = self.auth_path.read_bytes()
        completed = self.run_device_login(fail=True)
        self.assertEqual(completed.returncode, 1)
        events = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(events[-1]["event"], "failed")
        self.assertEqual(events[-1]["error"], "Fixture authorization failed")
        self.assertEqual(self.auth_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
