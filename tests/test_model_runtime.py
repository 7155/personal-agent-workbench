from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from rag_ime import model_runtime as model_runtime_module
from rag_ime.model_registry import ModelDeployment, fingerprint_model_artifact
from rag_ime.model_runtime import (
    MODEL_RUNTIME_PLAN_SCHEMA_VERSION,
    plan_model_runtime,
    probe_runtime_endpoint,
)


ROOT = Path(__file__).resolve().parents[1]


class ModelRuntimePlanTests(unittest.TestCase):
    def test_mlx_plan_manages_resident_service_and_exports_real_provider_env(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-runtime-mlx-") as tmp:
            model = Path(tmp) / "model"
            model.mkdir()
            for deployment_profile in (
                "minimind_ime_60m_v8",
                "minimind_ime_100m_v1",
                "minimind_ime_v2",
            ):
                with self.subTest(deployment_profile=deployment_profile):
                    deployment = ModelDeployment(
                        model_id="mini-q8",
                        path=str(model),
                        format="mlx",
                        fingerprint="sha256:test",
                        profile=deployment_profile,
                        runtime="mlx",
                        prompt_mode="base-completion",
                    )
                    plan = plan_model_runtime(deployment)

                    self.assertTrue(plan.ready)
                    self.assertEqual(plan.managed_service, "com.rag-ime.mlx-predictor")
                    self.assertEqual(plan.provider_env["RAG_IME_PREDICTOR_PROVIDER"], "mlx")
                    self.assertEqual(plan.provider_env["RAG_IME_PREDICTOR_MODEL"], str(model))
                    self.assertEqual(
                        plan.provider_env["RAG_IME_PREDICTOR_PROFILE"],
                        "minimind_ime_v2",
                    )
                    self.assertEqual(
                        plan.provider_env["RAG_IME_MLX_PROFILE"],
                        "minimind_ime_v2",
                    )
                    self.assertEqual(plan.deployment.profile, deployment_profile)
                    self.assertEqual(plan.provider_env["RAG_IME_PREDICTOR_MAX_TOKENS"], "8")
                    self.assertEqual(plan.provider_env["RAG_IME_PREDICTOR_TEMPERATURE"], "0.15")
                    self.assertEqual(plan.provider_env["RAG_IME_PREDICTOR_TOP_P"], "0.85")
                    self.assertEqual(plan.provider_env["RAG_IME_MLX_MAX_TOKENS"], "8")
                    self.assertTrue(plan.expected_capabilities["batchCandidates"])
                    self.assertEqual(plan.payload()["schemaVersion"], MODEL_RUNTIME_PLAN_SCHEMA_VERSION)

    def test_mlx_plan_exports_registered_generation_tuning(self) -> None:
        deployment = ModelDeployment(
            model_id="mini-custom",
            path="/tmp/minimind-ime-v2",
            format="mlx",
            fingerprint="sha256:test",
            profile="minimind_ime_v2",
            runtime="mlx",
            prompt_mode="base-completion",
            max_tokens=12,
            temperature=0.2,
            top_p=0.9,
        )

        plan = plan_model_runtime(deployment)

        self.assertEqual(plan.provider_env["RAG_IME_PREDICTOR_MAX_TOKENS"], "12")
        self.assertEqual(plan.provider_env["RAG_IME_PREDICTOR_TEMPERATURE"], "0.2")
        self.assertEqual(plan.provider_env["RAG_IME_PREDICTOR_TOP_P"], "0.9")
        self.assertEqual(plan.provider_env["RAG_IME_MLX_MAX_TOKENS"], "12")
        self.assertEqual(plan.provider_env["RAG_IME_MLX_TEMPERATURE"], "0.2")
        self.assertEqual(plan.provider_env["RAG_IME_MLX_TOP_P"], "0.9")

    def test_ollama_plan_is_external_and_uses_model_name_not_artifact_path(self) -> None:
        deployment = ModelDeployment(
            model_id="qwen-ollama",
            path="unused.gguf",
            format="gguf",
            fingerprint="runtime:ollama",
            profile="qwen3_06b_ime_hot",
            runtime="ollama",
            endpoint="http://127.0.0.1:11434",
            model_name="qwen3:0.6b",
        )

        plan = plan_model_runtime(deployment)

        self.assertTrue(plan.ready)
        self.assertEqual(plan.lifecycle, "external-ollama")
        self.assertEqual(plan.provider_env["RAG_IME_PREDICTOR_MODEL"], "qwen3:0.6b")
        self.assertEqual(plan.provider_env["RAG_IME_PREDICTOR_STREAM_FIRST"], "1")

    def test_external_openai_hot_runtime_rejects_remote_endpoint(self) -> None:
        deployment = ModelDeployment(
            model_id="bad-remote",
            path="model",
            format="remote",
            fingerprint="remote",
            profile="custom",
            runtime="openai-compatible",
            endpoint="https://example.com/v1",
            model_name="model",
        )

        plan = plan_model_runtime(deployment)

        self.assertFalse(plan.ready)
        self.assertIn("loopback", " ".join(plan.errors))

    def test_external_openai_hot_runtime_rejects_explicit_port_zero(self) -> None:
        deployment = _external_deployment(
            runtime="openai-compatible",
            endpoint="http://127.0.0.1:0/v1",
            model_name="demo-model",
        )

        plan = plan_model_runtime(deployment)

        self.assertFalse(plan.ready)
        self.assertIn("loopback", " ".join(plan.errors))

    def test_ollama_probe_normalizes_v1_endpoint_to_native_api(self) -> None:
        with _json_server({"/api/tags": {"models": [{"name": "qwen3:0.6b"}]}}) as (base_url, requests):
            deployment = _external_deployment(
                runtime="ollama",
                endpoint=f"{base_url}/v1",
                model_name="qwen3:0.6b",
                artifact_format="gguf",
            )

            probe = probe_runtime_endpoint(plan_model_runtime(deployment), timeout_s=1.0)

        self.assertTrue(probe["ok"], probe)
        self.assertEqual(requests, ["/api/tags"])

    def test_ollama_probe_rejects_reachable_server_without_selected_model(self) -> None:
        with _json_server({"/api/tags": {"models": []}}) as (base_url, _requests):
            deployment = _external_deployment(
                runtime="ollama",
                endpoint=base_url,
                model_name="missing:latest",
                artifact_format="gguf",
            )

            probe = probe_runtime_endpoint(plan_model_runtime(deployment), timeout_s=1.0)

        self.assertFalse(probe["ok"], probe)

    def test_mlx_probe_rejects_health_payload_when_model_is_not_loaded(self) -> None:
        with _json_server({"/health": {"ok": False, "modelLoaded": False}}) as (base_url, _requests):
            with tempfile.TemporaryDirectory(prefix="rag-ime-runtime-unloaded-mlx-") as tmp:
                model = Path(tmp) / "model"
                model.mkdir()
                deployment = ModelDeployment(
                    model_id="unloaded-mlx",
                    path=str(model),
                    format="mlx",
                    fingerprint="sha256:test",
                    profile="minimind_ime_v2",
                    runtime="mlx",
                    endpoint=base_url,
                )

                probe = probe_runtime_endpoint(plan_model_runtime(deployment), timeout_s=1.0)

        self.assertFalse(probe["ok"], probe)

    def test_mlx_probe_verifies_loaded_artifact_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-runtime-identity-mlx-") as tmp:
            model = Path(tmp) / "selected"
            model.mkdir()
            (model / "config.json").write_text("{}", encoding="utf-8")
            (model / "model.safetensors").write_bytes(b"selected weights")
            fingerprint = fingerprint_model_artifact(model)
            health = {
                "ok": True,
                "modelLoaded": True,
                "model": str(model.resolve()),
                "modelFingerprint": fingerprint,
            }
            with _json_server({"/health": health}) as (base_url, _requests):
                deployment = ModelDeployment(
                    model_id="selected-mlx",
                    path=str(model),
                    format="mlx",
                    fingerprint=fingerprint,
                    profile="minimind_ime_v2",
                    runtime="mlx",
                    endpoint=base_url,
                )
                probe = probe_runtime_endpoint(plan_model_runtime(deployment), timeout_s=1.0)

        self.assertTrue(probe["ok"], probe)
        self.assertTrue(probe["modelIdentity"]["verified"])
        self.assertEqual(probe["modelIdentity"]["mode"], "artifact-fingerprint")

    def test_mlx_probe_keeps_legacy_short_fingerprint_compatible_when_path_matches(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-runtime-legacy-fingerprint-") as tmp:
            model = Path(tmp) / "selected"
            model.mkdir()
            (model / "config.json").write_text("{}", encoding="utf-8")
            (model / "model.safetensors").write_bytes(b"selected weights")
            health = {
                "ok": True,
                "modelLoaded": True,
                "model": str(model.resolve()),
                "modelFingerprint": fingerprint_model_artifact(model),
            }
            with _json_server({"/health": health}) as (base_url, _requests):
                deployment = ModelDeployment(
                    model_id="legacy-mlx",
                    path=str(model),
                    format="mlx",
                    fingerprint="sha256:0000000000000000",
                    profile="minimind_ime_v2",
                    runtime="mlx",
                    endpoint=base_url,
                )
                probe = probe_runtime_endpoint(plan_model_runtime(deployment), timeout_s=1.0)

        self.assertTrue(probe["ok"], probe)
        self.assertEqual(probe["modelIdentity"]["registryFingerprintStatus"], "legacy-unverified")

    def test_mlx_probe_rejects_old_resident_model_after_registry_switch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-runtime-stale-mlx-") as tmp:
            root = Path(tmp)
            selected = root / "selected"
            resident = root / "resident"
            for model, weights in ((selected, b"new weights"), (resident, b"old weights")):
                model.mkdir()
                (model / "config.json").write_text("{}", encoding="utf-8")
                (model / "model.safetensors").write_bytes(weights)
            health = {
                "ok": True,
                "modelLoaded": True,
                "model": str(resident.resolve()),
                "modelFingerprint": fingerprint_model_artifact(resident),
            }
            with _json_server({"/health": health}) as (base_url, _requests):
                deployment = ModelDeployment(
                    model_id="new-mlx",
                    path=str(selected),
                    format="mlx",
                    fingerprint=fingerprint_model_artifact(selected),
                    profile="minimind_ime_v2",
                    runtime="mlx",
                    endpoint=base_url,
                )
                probe = probe_runtime_endpoint(plan_model_runtime(deployment), timeout_s=1.0)

        self.assertFalse(probe["ok"], probe)
        self.assertEqual(probe["error"], "loaded_model_fingerprint_mismatch")

    def test_openai_compatible_plan_normalizes_v1_for_existing_provider_contract(self) -> None:
        deployment = _external_deployment(
            runtime="openai-compatible",
            endpoint="http://127.0.0.1:18000/v1",
            model_name="demo-model",
        )

        plan = plan_model_runtime(deployment)

        self.assertEqual(plan.provider_env["RAG_IME_PREDICTOR_BASE_URL"], "http://127.0.0.1:18000")

    def test_openai_compatible_probe_uses_configured_local_api_key(self) -> None:
        with _authenticated_json_server("local-secret") as base_url:
            deployment = _external_deployment(
                runtime="openai-compatible",
                endpoint=base_url,
                model_name="demo-model",
            )
            with patch.dict(os.environ, {"RAG_IME_PREDICTOR_API_KEY": "local-secret"}):
                probe = probe_runtime_endpoint(plan_model_runtime(deployment), timeout_s=1.0)

        self.assertTrue(probe["ok"], probe)

    def test_openai_compatible_probe_rejects_server_without_selected_model(self) -> None:
        with _json_server({"/v1/models": {"data": [{"id": "other-model"}]}}) as (base_url, _requests):
            deployment = _external_deployment(
                runtime="openai-compatible",
                endpoint=base_url,
                model_name="missing-model",
            )

            probe = probe_runtime_endpoint(plan_model_runtime(deployment), timeout_s=1.0)

        self.assertFalse(probe["ok"], probe)

    def test_openai_compatible_probe_rejects_empty_model_list(self) -> None:
        empty_models = {"data": []}
        with _json_server({"/v1/models": empty_models, "/models": empty_models}) as (base_url, _requests):
            deployment = _external_deployment(
                runtime="openai-compatible",
                endpoint=base_url,
                model_name="demo-model",
            )

            probe = probe_runtime_endpoint(plan_model_runtime(deployment), timeout_s=1.0)

        self.assertFalse(probe["ok"], probe)
        self.assertEqual(probe["error"], "selected_model_not_available")

    def test_openai_compatible_probe_reads_api_key_from_predictor_env_without_leaking_it(self) -> None:
        token = "predictor-env-secret"
        with _authenticated_json_server(token) as base_url:
            deployment = _external_deployment(
                runtime="openai-compatible",
                endpoint=base_url,
                model_name="demo-model",
            )
            with tempfile.TemporaryDirectory(prefix="rag-ime-runtime-predictor-env-") as tmp:
                env_path = Path(tmp) / "predictor.env"
                env_path.write_text(f"RAG_IME_PREDICTOR_API_KEY={token}\n", encoding="utf-8")
                missing_plist = Path(tmp) / "missing-sidecar.plist"
                with patch.dict(
                    os.environ,
                    {
                        "RAG_IME_PREDICTOR_ENV": str(env_path),
                        "RAG_IME_SIDECAR_PLIST": str(missing_plist),
                    },
                    clear=True,
                ):
                    probe = probe_runtime_endpoint(plan_model_runtime(deployment), timeout_s=1.0)

        self.assertTrue(probe["ok"], probe)
        self.assertNotIn(token, json.dumps(probe, ensure_ascii=False))

    def test_openai_compatible_probe_reads_api_key_from_existing_sidecar_plist_without_leaking_it(self) -> None:
        token = "sidecar-plist-secret"
        with _authenticated_json_server(token) as base_url:
            deployment = _external_deployment(
                runtime="openai-compatible",
                endpoint=base_url,
                model_name="demo-model",
            )
            with tempfile.TemporaryDirectory(prefix="rag-ime-runtime-sidecar-plist-") as tmp:
                plist_path = Path(tmp) / "com.rag-ime.sidecar.plist"
                with plist_path.open("wb") as handle:
                    plistlib.dump(
                        {
                            "EnvironmentVariables": {
                                "RAG_IME_PREDICTOR_API_KEY": token,
                            }
                        },
                        handle,
                    )
                with patch.dict(
                    os.environ,
                    {"RAG_IME_SIDECAR_PLIST": str(plist_path)},
                    clear=True,
                ):
                    probe = probe_runtime_endpoint(plan_model_runtime(deployment), timeout_s=1.0)

        self.assertTrue(probe["ok"], probe)
        self.assertNotIn(token, json.dumps(probe, ensure_ascii=False))

    def test_runtime_probe_opener_ignores_proxy_environment(self) -> None:
        proxy_handlers = [
            handler
            for handler in model_runtime_module._NO_PROXY_OPENER.handlers
            if isinstance(handler, urllib.request.ProxyHandler)
        ]
        # ProxyHandler({}) intentionally installs no proxy handlers at all.
        self.assertEqual(proxy_handlers, [])

        with _json_server({"/v1/models": {"data": [{"id": "demo-model"}]}}) as (base_url, _requests):
            deployment = _external_deployment(
                runtime="openai-compatible",
                endpoint=base_url,
                model_name="demo-model",
            )
            hostile_proxy_env = {
                "HTTP_PROXY": "http://127.0.0.1:1",
                "HTTPS_PROXY": "http://127.0.0.1:1",
                "ALL_PROXY": "http://127.0.0.1:1",
                "NO_PROXY": "",
                "no_proxy": "",
            }
            with patch.dict(os.environ, hostile_proxy_env):
                probe = probe_runtime_endpoint(plan_model_runtime(deployment), timeout_s=1.0)

        self.assertTrue(probe["ok"], probe)

    def test_restart_fails_closed_when_registered_active_mlx_artifact_is_missing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-runtime-missing-active-") as tmp:
            root = Path(tmp)
            home = root / "home"
            fallback = root / "fallback" / "minimind-ime-v2"
            fallback.mkdir(parents=True)
            registry_path = _write_registry(
                home,
                ModelDeployment(
                    model_id="missing-active",
                    path=str(root / "missing-model"),
                    format="mlx",
                    fingerprint="sha256:missing",
                    profile="minimind_ime_v2",
                    runtime="mlx",
                    endpoint="http://127.0.0.1:8767",
                    prompt_mode="base-completion",
                ),
            )
            env = _restart_env(home)
            env["RAG_IME_MODEL_REGISTRY"] = str(registry_path)
            env["RAG_IME_MODELS_DIR"] = str(fallback.parent)

            result = _run_restart(ROOT, env)

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("model", result.stderr.lower())

    def test_restart_fails_closed_when_explicit_registry_path_does_not_exist(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-runtime-missing-registry-") as tmp:
            root = Path(tmp)
            home = root / "home"
            fallback = root / "fallback" / "minimind-ime-v2"
            fallback.mkdir(parents=True)
            env = _restart_env(home)
            env["RAG_IME_MODEL_REGISTRY"] = str(root / "missing-models.json")
            env["RAG_IME_MODELS_DIR"] = str(fallback.parent)

            result = _run_restart(ROOT, env)

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("registry", result.stderr.lower())

    def test_restart_rejects_relative_sidecar_python_before_runtime_resolution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-runtime-relative-python-") as tmp:
            home = Path(tmp) / "home"
            env = _restart_env(home)
            env["RAG_IME_PYTHON"] = "python3"

            result = _run_restart(ROOT, env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RAG_IME_PYTHON must be an absolute executable file", result.stderr)

    def test_restart_keeps_mlx_launch_port_and_sidecar_route_consistent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-runtime-mlx-endpoint-") as tmp:
            root = Path(tmp)
            home = root / "home"
            model = root / "model"
            model.mkdir()
            (model / "config.json").write_text("{}", encoding="utf-8")
            registry_path = _write_registry(
                home,
                ModelDeployment(
                    model_id="mlx-custom-port",
                    path=str(model),
                    format="mlx",
                    fingerprint="sha256:test",
                    profile="minimind_ime_v2",
                    runtime="mlx",
                    endpoint="http://127.0.0.1:18999",
                    prompt_mode="base-completion",
                ),
            )
            env = _restart_env(home)
            env["RAG_IME_MODEL_REGISTRY"] = str(registry_path)

            result = _run_restart(ROOT, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            mlx = _load_plist(home / "Library" / "LaunchAgents" / "com.rag-ime.mlx-predictor.plist")
            sidecar = _load_plist(home / "Library" / "LaunchAgents" / "com.rag-ime.sidecar.plist")

        mlx_port = int(mlx["EnvironmentVariables"]["RAG_IME_MLX_PORT"])
        sidecar_port = urlsplit(sidecar["EnvironmentVariables"]["RAG_IME_PREDICTOR_BASE_URL"]).port
        self.assertEqual(mlx_port, sidecar_port)

    def test_legacy_explicit_qwen_model_keeps_qwen_profile_without_registry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-runtime-legacy-qwen-") as tmp:
            root = Path(tmp)
            home = root / "home"
            model = root / "mlx-community-Qwen3-0.6B-4bit-local"
            model.mkdir()
            (model / "config.json").write_text(
                json.dumps({"architectures": ["Qwen3ForCausalLM"], "model_type": "qwen3"}),
                encoding="utf-8",
            )
            env = _restart_env(home)
            env["RAG_IME_MLX_MODEL"] = str(model)

            result = _run_restart(ROOT, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            mlx = _load_plist(home / "Library" / "LaunchAgents" / "com.rag-ime.mlx-predictor.plist")

        self.assertEqual(mlx["EnvironmentVariables"]["RAG_IME_MLX_PROFILE"], "qwen3_06b_ime_hot")

    def test_legacy_mlx_port_override_keeps_sidecar_route_consistent_without_registry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-runtime-legacy-port-") as tmp:
            root = Path(tmp)
            home = root / "home"
            model = root / "minimind-ime-v2"
            model.mkdir()
            (model / "config.json").write_text("{}", encoding="utf-8")
            env = _restart_env(home)
            env["RAG_IME_MLX_MODEL"] = str(model)
            env["RAG_IME_MLX_PORT"] = "18998"

            result = _run_restart(ROOT, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            mlx = _load_plist(home / "Library" / "LaunchAgents" / "com.rag-ime.mlx-predictor.plist")
            sidecar = _load_plist(home / "Library" / "LaunchAgents" / "com.rag-ime.sidecar.plist")

        self.assertEqual(mlx["EnvironmentVariables"]["RAG_IME_MLX_PORT"], "18998")
        self.assertEqual(
            urlsplit(sidecar["EnvironmentVariables"]["RAG_IME_PREDICTOR_BASE_URL"]).port,
            18998,
        )

    def test_restart_propagates_external_predictor_prompt_mode_to_sidecar(self) -> None:
        with _json_server({"/v1/models": {"data": [{"id": "demo-model"}]}}) as (base_url, _requests):
            with tempfile.TemporaryDirectory(prefix="rag-ime-runtime-prompt-mode-") as tmp:
                root = Path(tmp)
                home = root / "home"
                fake_bin = _fake_launchctl_bin(root)
                registry_path = _write_registry(
                    home,
                    _external_deployment(
                        runtime="openai-compatible",
                        endpoint=base_url,
                        model_name="demo-model",
                        prompt_mode="base-completion",
                    ),
                )
                env = _restart_env(home)
                env["PATH"] = f"{fake_bin}:{env['PATH']}"
                env["RAG_IME_MODEL_REGISTRY"] = str(registry_path)

                result = _run_restart(ROOT, env)
                self.assertEqual(result.returncode, 0, result.stderr)
                sidecar = _load_plist(home / "Library" / "LaunchAgents" / "com.rag-ime.sidecar.plist")

        self.assertEqual(
            sidecar["EnvironmentVariables"].get("RAG_IME_PREDICTOR_PROMPT_MODE"),
            "base-completion",
        )

    def test_external_restart_does_not_require_an_mlx_virtualenv(self) -> None:
        with _json_server({"/api/tags": {"models": [{"name": "qwen3:0.6b"}]}}) as (base_url, _requests):
            with tempfile.TemporaryDirectory(prefix="rag-ime-runtime-no-mlx-venv-") as tmp:
                root = Path(tmp)
                runtime_root = root / "checkout"
                scripts = runtime_root / "scripts"
                scripts.mkdir(parents=True)
                (scripts / "support").mkdir()
                shutil.copy2(
                    ROOT / "scripts" / "support" / "prebuilt_product.sh",
                    scripts / "support" / "prebuilt_product.sh",
                )
                shutil.copytree(ROOT / "rag_ime", runtime_root / "rag_ime")
                shutil.copytree(ROOT / "integrations" / "pi", runtime_root / "integrations" / "pi")
                (runtime_root / "examples").mkdir()
                shutil.copytree(
                    ROOT / "examples" / "vertical_agents",
                    runtime_root / "examples" / "vertical_agents",
                )
                # Portable controls are a prebuilt installer input; this probe
                # exercises predictor switching without a frontend toolchain.
                portable_ui = runtime_root / "control-center-web" / ".generated" / "portable-agent-ui"
                portable_ui.mkdir(parents=True)
                (portable_ui / "agent-ui.js").write_text("export {};\n", encoding="utf-8")
                (portable_ui / "agent-ui.css").write_text(":root {}\n", encoding="utf-8")
                eval_metrics = runtime_root / "eval" / "interview-metrics"
                eval_metrics.mkdir(parents=True)
                shutil.copy2(
                    ROOT / "eval" / "interview-metrics" / "agent-experiments.v1.json",
                    eval_metrics / "agent-experiments.v1.json",
                )
                from scripts.list_agent_lab_install_receipts import required_receipts
                (eval_metrics / "runs").mkdir()
                for receipt in required_receipts(ROOT / "eval/interview-metrics/agent-experiments.v1.json"):
                    shutil.copy2(receipt, eval_metrics / "runs" / receipt.name)
                for name in (
                    "list_agent_lab_install_receipts.py",
                    "restart_rag_ime_runtime.sh",
                    "install_sidecar_launch_agent.sh",
                    "import_agent_lab_experiments.py",
                    "sidecar_launch.py",
                    "portable_restore_supervisor.py",
                ):
                    shutil.copy2(ROOT / "scripts" / name, scripts / name)
                home = root / "home"
                fake_bin = _fake_launchctl_bin(root, with_python=True)
                registry_path = _write_registry(
                    home,
                    _external_deployment(
                        runtime="ollama",
                        endpoint=base_url,
                        model_name="qwen3:0.6b",
                        artifact_format="gguf",
                    ),
                )
                env = _restart_env(home, explicit_python=False)
                env["RAG_IME_LAUNCH_AGENT_DRY_RUN"] = "true"
                env["PATH"] = f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin"
                env["RAG_IME_MODEL_REGISTRY"] = str(registry_path)
                env["RAG_IME_MLX_PYTHON"] = str(root / "missing-mlx-venv" / "bin" / "python")

                result = _run_restart(runtime_root, env)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_external_probe_does_not_stop_previous_mlx_runtime(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-runtime-transaction-") as tmp:
            root = Path(tmp)
            home = root / "home"
            fake_bin = _fake_launchctl_bin(root)
            launchctl_log = root / "launchctl.log"
            launchctl = fake_bin / "launchctl"
            launchctl.write_text(
                f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {str(launchctl_log)!r}\nexit 0\n",
                encoding="utf-8",
            )
            launchctl.chmod(0o755)
            registry_path = _write_registry(
                home,
                _external_deployment(
                    runtime="ollama",
                    endpoint="http://127.0.0.1:9",
                    model_name="qwen3:0.6b",
                    artifact_format="gguf",
                ),
            )
            env = _restart_env(home)
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["RAG_IME_MODEL_REGISTRY"] = str(registry_path)

            result = _run_restart(ROOT, env)
            calls = launchctl_log.read_text(encoding="utf-8") if launchctl_log.exists() else ""

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("bootout", calls)
        self.assertNotIn("com.rag-ime.mlx-predictor", calls)

    def test_failed_sidecar_switch_restores_previous_plist_and_keeps_old_mlx_running(self) -> None:
        with _json_server({"/api/tags": {"models": [{"name": "qwen3:0.6b"}]}}) as (base_url, _requests):
            with tempfile.TemporaryDirectory(prefix="rag-ime-runtime-sidecar-rollback-") as tmp:
                root = Path(tmp)
                runtime_root = root / "checkout"
                scripts = runtime_root / "scripts"
                scripts.mkdir(parents=True)
                shutil.copytree(ROOT / "rag_ime", runtime_root / "rag_ime")
                shutil.copy2(ROOT / "scripts" / "restart_rag_ime_runtime.sh", scripts)

                failing_installer = scripts / "install_sidecar_launch_agent.sh"
                failing_installer.write_text(
                    "#!/bin/sh\n"
                    'printf "%s" "replacement" > "$HOME/Library/LaunchAgents/com.rag-ime.sidecar.plist"\n'
                    "exit 23\n",
                    encoding="utf-8",
                )
                failing_installer.chmod(0o755)

                home = root / "home"
                sidecar_plist = home / "Library" / "LaunchAgents" / "com.rag-ime.sidecar.plist"
                sidecar_plist.parent.mkdir(parents=True)
                with sidecar_plist.open("wb") as handle:
                    plistlib.dump(
                        {
                            "Label": "com.rag-ime.sidecar",
                            "EnvironmentVariables": {"RAG_IME_MODEL_ID": "previous-mlx"},
                        },
                        handle,
                    )
                previous_plist = sidecar_plist.read_bytes()

                registry_path = _write_registry(
                    home,
                    _external_deployment(
                        runtime="ollama",
                        endpoint=base_url,
                        model_name="qwen3:0.6b",
                        artifact_format="gguf",
                    ),
                )
                fake_bin = _fake_launchctl_bin(root)
                launchctl_log = root / "launchctl.log"
                launchctl = fake_bin / "launchctl"
                launchctl.write_text(
                    '#!/bin/sh\nprintf "%s\\n" "$*" >> "$RAG_IME_TEST_LAUNCHCTL_LOG"\nexit 0\n',
                    encoding="utf-8",
                )
                launchctl.chmod(0o755)

                env = _restart_env(home)
                env.update(
                    {
                        "PATH": f"{fake_bin}:{env['PATH']}",
                        "RAG_IME_MODEL_REGISTRY": str(registry_path),
                        "RAG_IME_LAUNCH_AGENT_DRY_RUN": "0",
                        "RAG_IME_TEST_LAUNCHCTL_LOG": str(launchctl_log),
                    }
                )

                result = _run_restart(runtime_root, env)
                calls = launchctl_log.read_text(encoding="utf-8") if launchctl_log.exists() else ""

                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(sidecar_plist.read_bytes(), previous_plist)
                self.assertIn("com.rag-ime.sidecar", calls)
                self.assertNotIn("com.rag-ime.mlx-predictor", calls)
                self.assertIn("previous MLX runtime was left running", result.stderr)


def _external_deployment(
    *,
    runtime: str,
    endpoint: str,
    model_name: str,
    artifact_format: str = "remote",
    prompt_mode: str = "",
) -> ModelDeployment:
    return ModelDeployment(
        model_id=f"{runtime}-test",
        path=model_name,
        format=artifact_format,
        fingerprint=f"runtime:{runtime}:test",
        profile="custom",
        runtime=runtime,
        endpoint=endpoint,
        model_name=model_name,
        prompt_mode=prompt_mode,
    )


def _write_registry(home: Path, deployment: ModelDeployment) -> Path:
    registry_path = home / "Library" / "Application Support" / "RagIme" / "models.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {
                "schemaVersion": "rag-ime.model-registry.v2",
                "revision": 1,
                "models": [deployment.payload()],
            }
        ),
        encoding="utf-8",
    )
    return registry_path


def _restart_env(home: Path, *, explicit_python: bool = True) -> dict[str, str]:
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("RAG_IME_"):
            env.pop(key)
    env.update(
        {
            "HOME": str(home),
            "RAG_IME_LAUNCH_AGENT_DRY_RUN": "1",
            "RAG_IME_MLX_LAUNCH_AGENT_DRY_RUN": "1",
        }
    )
    if explicit_python:
        env["RAG_IME_PYTHON"] = sys.executable
        env["RAG_IME_MLX_PYTHON"] = sys.executable
    return env


def _run_restart(root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "scripts" / "restart_rag_ime_runtime.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )


def _load_plist(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        return plistlib.load(handle)


def _fake_launchctl_bin(root: Path, *, with_python: bool = False) -> Path:
    fake_bin = root / "bin"
    fake_bin.mkdir(exist_ok=True)
    launchctl = fake_bin / "launchctl"
    launchctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launchctl.chmod(0o755)
    if with_python:
        (fake_bin / "python3").symlink_to(sys.executable)
    return fake_bin


@contextmanager
def _json_server(routes: dict[str, object]):
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
            requests.append(self.path)
            payload = routes.get(self.path)
            if payload is None:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: object) -> None:
            _ = args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextmanager
def _authenticated_json_server(token: str):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
            if self.headers.get("Authorization") != f"Bearer {token}":
                self.send_response(401)
                self.end_headers()
                return
            body = b'{"data":[{"id":"demo-model"}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: object) -> None:
            _ = args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
