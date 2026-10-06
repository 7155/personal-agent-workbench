from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ESBUILD_CANDIDATES = (
    ROOT.parent / "pi" / "node_modules" / ".bin" / "esbuild",
    ROOT / "control-center-web" / "node_modules" / ".pnpm" / "node_modules" / ".bin" / "esbuild",
)
ESBUILD = next((path for path in ESBUILD_CANDIDATES if path.is_file()), ESBUILD_CANDIDATES[0])
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
import { readFile, writeFile } from 'node:fs/promises';

export class AuthStorage {
  static create(path) {
    return { list: async () => [], modify: async (provider, update) => {
      const credential = await update();
      await writeFile(path, JSON.stringify({ provider, type: credential.type }));
    } };
  }
}
export class ModelRuntime {
  static async create(options) {
    if (options.allowModelNetwork !== false) throw new Error('Catalog network must remain disabled');
    if (!options.credentials) throw new Error('Existing AuthStorage must be used');
    const config = JSON.parse(await readFile(options.modelsPath, 'utf8'));
    const runtime = new ModelRuntime();
    runtime.models = Object.entries(config.providers).flatMap(([provider, definition]) =>
      definition.models.map(model => ({ ...model, provider, baseUrl: model.baseUrl ?? definition.baseUrl })));
    return runtime;
  }
  getProviders() {
    return [...new Set(this.models.map(model => model.provider))].map(id => ({
      id, name: id, auth: { oauth: true, apiKey: false },
    }));
  }
  getAvailableSnapshot() { return this.models; }
  getModels(provider) { return this.models.filter(model => model.provider === provider); }
  getProviderAuthStatus() { return { configured: true, source: 'fixture' }; }
  getError() { return ''; }
}
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

    def run_catalog(self, models: list[dict[str, object]]) -> dict[str, Any]:
        (self.root / "models.json").write_text(json.dumps({
            "providers": {"openai-codex": {"models": models}},
        }), encoding="utf-8")
        environment = {
            key: os.environ[key]
            for key in ("PATH", "LANG", "TMPDIR")
            if key in os.environ
        }
        before = self.auth_path.read_bytes()
        completed = subprocess.run(
            [str(NODE), str(self.bridge)],
            input=json.dumps({"action": "catalog", "agentDir": str(self.root)}),
            capture_output=True, text=True, timeout=5, env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(self.auth_path.read_bytes(), before)
        return json.loads(completed.stdout)

    def test_catalog_exposes_non_secret_effective_model_limits_and_costs(self) -> None:
        catalog = self.run_catalog([{
            "id": "gpt-6.1-sol", "name": "Fixture model", "reasoning": True,
            "input": ["text", "image"], "api": "openai-codex-responses",
            "baseUrl": "https://chatgpt.com/backend-api", "contextWindow": 250_000,
            "maxTokens": 64_000,
            "cost": {"input": 2, "output": 10, "cacheRead": 0.2, "cacheWrite": 0,
                     "tiers": [{"input": 4, "output": 15, "cacheRead": 0.4, "cacheWrite": 5}]},
        }])
        self.assertTrue(catalog["ok"])
        provider = catalog["providers"][0]
        self.assertTrue(provider["configuredInCatalog"])
        self.assertEqual(provider["availableModels"], [{
            "id": "gpt-6.1-sol", "name": "Fixture model", "provider": "openai-codex",
            "api": "openai-codex-responses", "contextWindow": 250_000, "maxTokens": 64_000,
            "cost": {"input": 2, "output": 10, "cacheRead": 0.2, "cacheWrite": 0,
                     "tiers": [{"input": 4, "output": 15, "cacheRead": 0.4, "cacheWrite": 5}]},
            "reasoning": True, "imageInput": True,
        }])

    def test_catalog_omits_private_fields_and_retains_unknown_prices_as_null(self) -> None:
        sentinel = "private-credential-do-not-echo"
        catalog = self.run_catalog([{
            "id": "unsafe", "name": "Fixture model", "api": "openai-codex-responses",
            "baseUrl": f"https://user:{sentinel}@chatgpt.com/backend-api?key={sentinel}",
            "headers": {"Authorization": f"Bearer {sentinel}"},
            "apiKey": sentinel, "contextWindow": sentinel, "maxTokens": -1,
            "cost": {"input": sentinel, "output": -1, "cacheRead": None,
                     "cacheWrite": 0, "apiKey": sentinel,
                     "tiers": [{"input": 4, "cacheWrite": sentinel, "apiKey": sentinel}, None]},
        }])
        model = catalog["providers"][0]["availableModels"][0]
        self.assertNotIn("contextWindow", model)
        self.assertNotIn("maxTokens", model)
        self.assertEqual(model["cost"], {
            "input": None, "output": None, "cacheRead": None, "cacheWrite": 0,
            "tiers": [{"input": 4, "cacheWrite": None}, {"input": None}],
        })
        serialized = json.dumps(catalog)
        for private_field in (sentinel, "baseUrl", "headers", "Authorization"):
            self.assertNotIn(private_field, serialized)

    def test_catalog_unknown_or_malformed_pricing_stays_unknown(self) -> None:
        catalog = self.run_catalog([
            {"id": "missing", "name": "Fixture"},
            {"id": "invalid", "name": "Fixture", "cost": "private-invalid-price"},
            {"id": "malformed-tiers", "name": "Fixture",
             "cost": {"input": 2, "output": 10, "tiers": {"input": 100}}},
        ])
        for model in catalog["providers"][0]["availableModels"]:
            self.assertNotIn("cost", model)
        self.assertNotIn("private-invalid-price", json.dumps(catalog))


if __name__ == "__main__":
    unittest.main()
