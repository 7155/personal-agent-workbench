from __future__ import annotations

import json
import os
import plistlib
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class AgentLabInstallReceiptManifestTests(unittest.TestCase):
    def _run_manifest(
        self,
        tmp: str,
        *,
        refs: list[str],
        files: dict[str, str] | None = None,
        symlink: tuple[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        repository = Path(tmp) / "repository"
        metrics = repository / "eval" / "interview-metrics"
        runs = metrics / "runs"
        runs.mkdir(parents=True)
        for relative, content in (files or {}).items():
            path = repository / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        if symlink is not None:
            link, target = symlink
            (repository / link).symlink_to(repository / target)
        ledger = metrics / "agent-experiments.v1.json"
        ledger.write_text(json.dumps({"experiments": [{"evidenceRefs": refs}]}), encoding="utf-8")
        script = Path(__file__).resolve().parents[1] / "scripts" / "list_agent_lab_install_receipts.py"
        return subprocess.run(
            [sys.executable, str(script), str(ledger)],
            cwd=repository,
            check=False,
            text=True,
            capture_output=True,
        )

    def test_manifest_lists_only_existing_bound_receipts_and_optimal_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="paw-eval-install-manifest-") as tmp:
            result = self._run_manifest(
                tmp,
                refs=[
                    "eval/interview-metrics/runs/cost.json",
                    "eval/interview-metrics/runs/cost.json",
                ],
                files={
                    "eval/interview-metrics/runs/cost.json": "{}",
                    "eval/interview-metrics/runs/agent-lab-optimal-path-current.json": "{}",
                    "eval/interview-metrics/runs/unreferenced.json": "{}",
                },
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [Path(line).name for line in result.stdout.splitlines()],
            ["agent-lab-optimal-path-current.json", "cost.json"],
        )

    def test_manifest_rejects_a_missing_bound_receipt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="paw-eval-install-manifest-") as tmp:
            result = self._run_manifest(
                tmp,
                refs=["eval/interview-metrics/runs/missing.json"],
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid Agent Lab evidence ref", result.stderr)

    def test_manifest_rejects_a_traversal_receipt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="paw-eval-install-manifest-") as tmp:
            result = self._run_manifest(
                tmp,
                refs=["eval/interview-metrics/runs/../outside.json"],
                files={"eval/interview-metrics/outside.json": "{}"},
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid Agent Lab evidence ref", result.stderr)

    def test_manifest_rejects_a_receipt_symlink_outside_runs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="paw-eval-install-manifest-") as tmp:
            result = self._run_manifest(
                tmp,
                refs=["eval/interview-metrics/runs/linked.json"],
                files={"outside.json": "{}"},
                symlink=("eval/interview-metrics/runs/linked.json", "outside.json"),
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid Agent Lab evidence ref", result.stderr)



class LaunchAgentScriptTests(unittest.TestCase):
    def _sidecar_checkout(self) -> Path:
        """Keep real installer behavior independent of checkout build products."""
        source_root = Path(__file__).resolve().parents[1]
        temporary = tempfile.TemporaryDirectory(prefix="rag-ime-sidecar-checkout-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "checkout"
        scripts = root / "scripts"
        (scripts / "support").mkdir(parents=True)
        shutil.copy2(
            source_root / "scripts" / "support" / "prebuilt_product.sh",
            scripts / "support" / "prebuilt_product.sh",
        )
        for relative in ("rag_ime", "integrations/pi", "examples/vertical_agents"):
            shutil.copytree(
                source_root / relative,
                root / relative,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        for name in (
            "install_sidecar_launch_agent.sh",
            "sidecar_launch.py",
            "portable_restore_supervisor.py",
            "import_agent_lab_experiments.py",
            "list_agent_lab_install_receipts.py",
        ):
            shutil.copy2(source_root / "scripts" / name, scripts / name)
        from scripts.list_agent_lab_install_receipts import required_receipts

        ledger = source_root / "eval" / "interview-metrics" / "agent-experiments.v1.json"
        metrics = root / "eval" / "interview-metrics"
        (metrics / "runs").mkdir(parents=True)
        shutil.copy2(ledger, metrics / ledger.name)
        for receipt in required_receipts(ledger):
            shutil.copy2(receipt, metrics / "runs" / receipt.name)
        # UI and Browser compilation have their own build/runtime tests. These
        # tests exercise the real installer against bounded prebuilt inputs.
        portable_ui = root / "control-center-web" / ".generated" / "portable-agent-ui"
        portable_ui.mkdir(parents=True)
        (portable_ui / "agent-ui.js").write_text("export {};\n", encoding="utf-8")
        (portable_ui / "agent-ui.css").write_text(":root {}\n", encoding="utf-8")
        (scripts / "build_ego_browser_runtime.py").write_text(
            "raise SystemExit('installer tests must not build Browser runtime')\n",
            encoding="utf-8",
        )
        tools = root / "test-tools"
        tools.mkdir()
        for name in ("pnpm", "npm"):
            executable = tools / name
            executable.write_text(
                "#!/bin/sh\n"
                "echo 'installer tests must not build frontend assets' >&2\n"
                "exit 97\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("RAG_IME_", "DEEPSEEK_")) and key != "PAW_BINARY_PAYLOAD"
        }
        environment["PATH"] = f"{tools}:{environment.get('PATH', '')}"
        self.enterContext(patch.dict(os.environ, environment, clear=True))
        return root

    def test_sidecar_fixture_rejects_unexpected_frontend_compilation(self) -> None:
        root = self._sidecar_checkout()
        (root / "control-center-web" / ".generated" / "portable-agent-ui" / "agent-ui.js").unlink()
        with tempfile.TemporaryDirectory(prefix="rag-ime-sidecar-no-build-") as tmp:
            result = subprocess.run(
                ["bash", str(root / "scripts" / "install_sidecar_launch_agent.sh")],
                cwd=root,
                env={**os.environ, "HOME": tmp, "RAG_IME_PYTHON": sys.executable,
                     "RAG_IME_LAUNCH_AGENT_DRY_RUN": "1"},
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 97, result.stderr)
            self.assertIn("installer tests must not build frontend assets", result.stderr)
            self.assertFalse((Path(tmp) / "Library" / "LaunchAgents" / "com.rag-ime.sidecar.plist").exists())

    def test_sidecar_installer_manifest_failure_preserves_existing_app_tree(self) -> None:
        root = self._sidecar_checkout()
        with tempfile.TemporaryDirectory(prefix="rag-ime-launchd-manifest-failure-") as tmp:
            home = Path(tmp) / "home"
            app_dir = home / "Library" / "Application Support" / "RagIme" / "app"
            old_backend = app_dir / "rag_ime" / "old-backend.txt"
            old_evidence = app_dir / "eval" / "interview-metrics" / "old-evidence.txt"
            marker = app_dir / "rag-ime-install-marker.json"
            for path, content in (
                (old_backend, "old backend"),
                (old_evidence, "old evidence"),
                (marker, '{"generation":"old"}'),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            fake_python = Path(tmp) / "fail-agent-lab-manifest-python"
            fake_python.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in *list_agent_lab_install_receipts.py) exit 23 ;; esac\n"
                f"exec {shlex.quote(sys.executable)} \"$@\"\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            result = subprocess.run(
                ["bash", str(root / "scripts" / "install_sidecar_launch_agent.sh")],
                cwd=root,
                env={
                    **os.environ,
                    "HOME": str(home),
                    "RAG_IME_PYTHON": str(fake_python),
                    "RAG_IME_LAUNCH_AGENT_DRY_RUN": "1",
                    "RAG_IME_SIDECAR_PORT": "18765",
                },
                check=False,
                text=True,
                capture_output=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(old_backend.read_text(encoding="utf-8"), "old backend")
            self.assertEqual(old_evidence.read_text(encoding="utf-8"), "old evidence")
            self.assertEqual(marker.read_text(encoding="utf-8"), '{"generation":"old"}')

    def test_stop_runtime_proves_all_launch_agents_ports_and_processes_absent(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="rag-ime-stop-proof-") as tmp:
            home = Path(tmp) / "home"
            fake_bin = Path(tmp) / "bin"
            home.mkdir()
            fake_bin.mkdir()
            helpers = {
                "launchctl": "#!/bin/sh\n[ \"$1\" = print ] && exit 1\nexit 0\n",
                "lsof": "#!/bin/sh\nexit 1\n",
                "pgrep": "#!/bin/sh\nexit 1\n",
                "pkill": "#!/bin/sh\nexit 0\n",
                "sleep": "#!/bin/sh\nexit 0\n",
            }
            for name, source in helpers.items():
                path = fake_bin / name
                path.write_text(source, encoding="utf-8")
                path.chmod(0o755)
            env = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                "RAG_IME_DISABLE_FRONTEND_ON_STOP": "0",
            }
            result = subprocess.run(
                ["bash", str(root / "scripts" / "stop_rag_ime_runtime.sh")],
                cwd=root,
                env=env,
                check=False,
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("runtime stopped and LaunchAgents disabled", result.stdout)
        source = (root / "scripts" / "stop_rag_ime_runtime.sh").read_text(
            encoding="utf-8"
        )
        for process_name in (
            "RagImeDesktopBridge",
            "RagImeVoice",
            "RagImeControl",
            "Squirrel",
        ):
            self.assertIn(process_name, source)

    def test_sidecar_installer_auto_wires_stable_deepseek_env(self) -> None:
        root = self._sidecar_checkout()
        with tempfile.TemporaryDirectory(prefix="rag-ime-launchd-dsv4-") as tmp:
            home = Path(tmp)
            app_support = home / "Library" / "Application Support" / "RagIme"
            app_support.mkdir(parents=True)
            model_env = app_support / "deepseek.env"
            model_env.write_text(
                "DEEPSEEK_API_KEY=test-only\nRAG_IME_DEEPSEEK_MODEL=deepseek-v4-flash\n",
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "HOME": str(home),
                "RAG_IME_PYTHON": sys.executable,
                "RAG_IME_LAUNCH_AGENT_DRY_RUN": "1",
                "RAG_IME_SIDECAR_PORT": "18767",
                "RAG_IME_DEEPSEEK_ENV": "",
                "RAG_IME_MODEL_ENV": "",
                "RAG_IME_DEEPSEEK_ACTIVE_RAG": "",
            }
            subprocess.run(
                ["bash", str(root / "scripts" / "install_sidecar_launch_agent.sh")],
                cwd=root,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
            plist_path = home / "Library" / "LaunchAgents" / "com.rag-ime.sidecar.plist"
            with plist_path.open("rb") as fh:
                payload = plistlib.load(fh)

        launch_env = payload["EnvironmentVariables"]
        self.assertEqual(launch_env["RAG_IME_DEEPSEEK_ENV"], str(model_env))
        self.assertEqual(launch_env["RAG_IME_DEEPSEEK_ACTIVE_RAG"], "1")

    def test_sidecar_installer_copies_pi_provider_catalog_with_owner_only_permissions(self) -> None:
        root = self._sidecar_checkout()
        with tempfile.TemporaryDirectory(prefix="rag-ime-launchd-pi-provider-") as tmp:
            home = Path(tmp) / "home"
            source = Path(tmp) / "pikey.md"
            source.write_text('{"provider":{"openai":{"options":{"apiKey":"test-only"}}}}', encoding="utf-8")
            env = {
                **os.environ,
                "HOME": str(home),
                "RAG_IME_PYTHON": sys.executable,
                "RAG_IME_LAUNCH_AGENT_DRY_RUN": "1",
                "RAG_IME_SIDECAR_PORT": "18767",
                "RAG_IME_PI_PROVIDER_CONFIG": str(source),
            }
            subprocess.run(
                ["bash", str(root / "scripts" / "install_sidecar_launch_agent.sh")],
                cwd=root,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
            installed = home / "Library" / "Application Support" / "RagIme" / "pi-providers.json"
            plist_path = home / "Library" / "LaunchAgents" / "com.rag-ime.sidecar.plist"
            with plist_path.open("rb") as fh:
                payload = plistlib.load(fh)

            self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_PI_PROVIDER_CONFIG"], str(installed))
            self.assertEqual(installed.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
            self.assertEqual(installed.stat().st_mode & 0o777, 0o600)

    def test_install_frontend_launch_agent_dry_run_pins_user_app(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="rag-ime-frontend-launchd-test-") as tmp:
            home = Path(tmp)
            app = home / "Library" / "Input Methods" / "Squirrel.app"
            executable = app / "Contents" / "MacOS" / "Squirrel"
            executable.parent.mkdir(parents=True)
            (app / "Contents" / "Info.plist").write_text(
                "<plist><dict><key>CFBundleIdentifier</key><string>im.rime.inputmethod.Squirrel</string></dict></plist>",
                encoding="utf-8",
            )
            executable.write_text("#!/usr/bin/env bash\nsleep 60\n", encoding="utf-8")
            executable.chmod(0o755)
            env = {
                **os.environ,
                "HOME": str(home),
                "RAG_IME_FRONTEND_LAUNCH_AGENT_DRY_RUN": "1",
            }
            result = subprocess.run(
                ["bash", str(root / "scripts" / "install_frontend_launch_agent.sh")],
                cwd=root,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
            plist_path = home / "Library" / "LaunchAgents" / "com.rag-ime.frontend.plist"
            with plist_path.open("rb") as fh:
                payload = plistlib.load(fh)

        self.assertIn(str(plist_path), result.stdout)
        self.assertIn("dry-run", result.stdout)
        self.assertEqual(payload["Label"], "com.rag-ime.frontend")
        self.assertTrue(payload["RunAtLoad"])
        self.assertTrue(payload["KeepAlive"])
        self.assertEqual(payload["LimitLoadToSessionType"], "Aqua")
        self.assertEqual(payload["ProgramArguments"], [str(executable)])
        self.assertTrue(payload["WorkingDirectory"].endswith("Squirrel.app/Contents/MacOS"))
        self.assertTrue(payload["StandardOutPath"].endswith("Logs/RagIme/frontend.out.log"))
        self.assertTrue(payload["StandardErrorPath"].endswith("Logs/RagIme/frontend.err.log"))

    def test_install_sidecar_launch_agent_dry_run_writes_plist(self) -> None:
        root = self._sidecar_checkout()
        with tempfile.TemporaryDirectory(prefix="rag-ime-launchd-test-") as tmp:
            env = {
                **os.environ,
                "HOME": tmp,
                "RAG_IME_PYTHON": sys.executable,
                "RAG_IME_LAUNCH_AGENT_DRY_RUN": "1",
                "RAG_IME_SIDECAR_PORT": "18766",
                "RAG_IME_PREDICTOR_PROVIDER": "ollama",
                "RAG_IME_PREDICTOR_BASE_URL": "http://127.0.0.1:11434",
                "RAG_IME_PREDICTOR_MODEL": "qwen3.5:0.8b-mlx",
                "RAG_IME_PREDICTOR_PROFILE": "instant",
                "RAG_IME_PREDICTOR_STREAM_FIRST": "1",
                "RAG_IME_PREDICTOR_TIMEOUT_MS": "350",
                "RAG_IME_HISTORY_CONTEXT_EVENTS": "6",
                "RAG_IME_MODEL_CONTEXT_CHARS": "120",
                "RAG_IME_MODEL_LANE_MAX_CANDIDATES": "2",
                "RAG_IME_RIME_CACHE_TTL_MS": "400",
                "RAG_IME_EMBEDDING_PROVIDER": "openai-compatible",
                "RAG_IME_EMBEDDING_BASE_URL": "http://127.0.0.1:18000",
                "RAG_IME_EMBEDDING_MODEL": "bge-small-zh",
                "RAG_IME_EMBEDDING_WARMUP": "0",
                "RAG_IME_VECTOR_CANDIDATES": "48",
                "RAG_IME_VECTOR_WEIGHT": "1.7",
                "RAG_IME_VECTOR_AUTO_REBUILD_LIMIT": "5000",
                "RAG_IME_DEEPSEEK_BASE_URL": "https://api.kukuit.com",
                "RAG_IME_DEEPSEEK_API_KEY": "test-deepseek-key",
                "RAG_IME_DEEPSEEK_MODEL": "deepseek-v4-flash",
                "RAG_IME_DEEPSEEK_ACTIVE_RAG": "1",
                "RAG_IME_DEEPSEEK_ACTIVE_RAG_MAX_TOKENS": "1536",
            }
            env.pop("RAG_IME_EMBEDDING_WARMUP_DELAY_SECONDS", None)
            result = subprocess.run(
                ["bash", str(root / "scripts" / "install_sidecar_launch_agent.sh")],
                cwd=root,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
            plist_path = Path(tmp) / "Library" / "LaunchAgents" / "com.rag-ime.sidecar.plist"
            app_dir = Path(tmp) / "Library" / "Application Support" / "RagIme" / "app"
            self.assertIn(str(plist_path), result.stdout)
            self.assertIn("dry-run", result.stdout)
            self.assertTrue((app_dir / "rag_ime").is_dir())
            self.assertTrue((app_dir / "sidecar_launch.py").is_file())
            portable_ui = Path("control-center-web/.generated/portable-agent-ui")
            for name in ("agent-ui.js", "agent-ui.css"):
                self.assertEqual((app_dir / portable_ui / name).read_bytes(), (root / portable_ui / name).read_bytes())
            eval_lab_dir = app_dir / "eval" / "interview-metrics"
            self.assertTrue((eval_lab_dir / "agent-experiments.v1.json").is_file())
            self.assertTrue(
                (app_dir / "scripts" / "import_agent_lab_experiments.py").is_file()
            )
            self.assertTrue(
                (
                    eval_lab_dir
                    / "runs"
                    / "agent-lab-optimal-path-20260901.v1.json"
                ).is_file()
            )
            self.assertTrue(
                (
                    eval_lab_dir
                    / "runs"
                    / "agent-lab-cost-enterprise-rag-luna-max-coverage-balanced-v4-20260904.r4.v1.json"
                ).is_file()
            )
            projection = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import json, sys; "
                        "sys.path.insert(0, sys.argv[1]); "
                        "from rag_ime.eval_lab import EvalLabProjection; "
                        "print(json.dumps(EvalLabProjection(sys.argv[2], "
                        "source_ledger_path=sys.argv[3]).list_runs()))"
                    ),
                    str(app_dir),
                    str(Path(tmp) / "eval-lab-installed.sqlite"),
                    str(eval_lab_dir / "agent-experiments.v1.json"),
                ],
                cwd=app_dir,
                check=True,
                text=True,
                capture_output=True,
            )
            projection_payload = json.loads(projection.stdout)
            packaged_ledger = json.loads(
                (eval_lab_dir / "agent-experiments.v1.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                projection_payload["experimentTotal"],
                len(packaged_ledger["experiments"]),
            )
            packaged_path_searches = list(
                (eval_lab_dir / "runs").glob(
                    "agent-lab-optimal-path-*.json"
                )
            )
            self.assertEqual(
                projection_payload["pathSearchTotal"],
                len(packaged_path_searches),
            )
            init_prompt = (
                Path(tmp)
                / "Library"
                / "Application Support"
                / "RagIme"
                / "Agent"
                / "config"
                / "prompts"
                / "init.md"
            )
            self.assertTrue(init_prompt.is_file())
            init_prompt_text = init_prompt.read_text(encoding="utf-8")
            self.assertIn("AGENTS.md", init_prompt_text)
            self.assertIn("不得把动态 WorkItem", init_prompt_text)
            marker = json.loads((app_dir / "rag-ime-install-marker.json").read_text(encoding="utf-8"))
            with plist_path.open("rb") as fh:
                payload = plistlib.load(fh)

        self.assertEqual(marker["schemaVersion"], "rag-ime.component-install-marker.v1")
        self.assertEqual(marker["component"], "sidecar-runtime")
        self.assertEqual(marker["sourceRoot"], str(root))
        self.assertTrue(marker["sourceCommit"])
        self.assertEqual(payload["Label"], "com.rag-ime.sidecar")
        self.assertTrue(payload["RunAtLoad"])
        self.assertTrue(payload["KeepAlive"])
        self.assertNotIn("PYTHONPATH", payload["EnvironmentVariables"])
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_SOURCE_ROOT"], str(root))
        self.assertTrue(payload["EnvironmentVariables"]["RAG_IME_ROOT"].endswith("RagIme/app"))
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_PREDICTOR_PROVIDER"], "ollama")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_PREDICTOR_MODEL"], "qwen3.5:0.8b-mlx")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_PREDICTOR_STREAM_FIRST"], "1")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_HISTORY_CONTEXT_EVENTS"], "6")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_MODEL_CONTEXT_CHARS"], "120")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_MODEL_LANE_MAX_CANDIDATES"], "2")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_RIME_CACHE_TTL_MS"], "400")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_EMBEDDING_PROVIDER"], "openai-compatible")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_EMBEDDING_BASE_URL"], "http://127.0.0.1:18000")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_EMBEDDING_MODEL"], "bge-small-zh")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_EMBEDDING_WARMUP"], "0")
        self.assertEqual(
            payload["EnvironmentVariables"]["RAG_IME_EMBEDDING_WARMUP_DELAY_SECONDS"],
            "60",
        )
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_VECTOR_CANDIDATES"], "48")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_VECTOR_WEIGHT"], "1.7")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_VECTOR_AUTO_REBUILD_LIMIT"], "5000")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_DEEPSEEK_BASE_URL"], "https://api.kukuit.com")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_DEEPSEEK_API_KEY"], "test-deepseek-key")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_DEEPSEEK_MODEL"], "deepseek-v4-flash")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_DEEPSEEK_ACTIVE_RAG"], "1")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_DEEPSEEK_ACTIVE_RAG_MAX_TOKENS"], "1536")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_KNOWLEDGE_PYTHON"], sys.executable)
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_ENABLE_POST_COMMIT_ASYNC_COMPLETION"], "1")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_ENABLE_POST_COMMIT_AUTO_MODEL"], "1")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_ENABLE_COMPOSING_MODEL"], "0")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_ENABLE_PINYIN_CONSTRAINED_MODEL"], "0")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_POST_COMMIT_FIRST_RESPONSE_MS"], "180")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_PROGRESSIVE_FOLLOW_UP_RETRY_MS"], "180")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_POST_COMMIT_COMPLETION_TTL_MS"], "12000")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_POST_COMMIT_COMPLETION_CACHE_MAX_JOBS"], "8")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_POST_COMMIT_MODEL_HARD_TIMEOUT_MS"], "12000")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_POST_COMMIT_MODEL_BUDGET_MS"], "900")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_REQUIRE_FOREGROUND_CONTEXT_FOR_POST_COMMIT"], "1")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_FOREGROUND_CONTEXT_MAX_FRESHNESS_MS"], "700")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_PROGRESSIVE_FOREGROUND_CONTEXT_MAX_FRESHNESS_MS"], "2500")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_POST_COMMIT_PENDING_PREVIEW"], "1")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_POST_COMMIT_PRESENTATION_STREAM"], "0")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_ENABLE_DEMO_SAFE_FALLBACK"], "0")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_MODEL_HOLDOVER_MAX_ENTRIES"], "32")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_PREDICTION_MANAGER_MAX_ENTRIES"], "16")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_REFRESH_DEBOUNCE_MAX_ENTRIES"], "128")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_POST_COMMIT_PRESENTATION_STREAM_MAX_ENTRIES"], "32")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_SUGGESTION_CACHE_SIZE"], "32")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_RAG_DIRECT_DISPLAY"], "1")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_POST_COMMIT_ACTIVE_RAG_BUTTON"], "1")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_PI_ENABLED"], "0")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_AGENT_GATEWAY_ENABLED"], "1")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_KNOWLEDGE_SHARED_WORKER"], "1")
        self.assertNotIn("RAG_IME_ROOM_KERNEL_MODE", payload["EnvironmentVariables"])

        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_AUTO_PREDICT_IDLE_MS"], "180")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_AUTO_PREDICT_MIN_DELTA_CHARS"], "3")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_AUTO_PREDICT_MAX_CALLS_PER_10S"], "6")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_AUTO_PREDICT_IGNORE_COOLDOWN_MS"], "600")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_LOCAL_MODEL_QUALITY_GATE_MODE"], "observe")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_DEEPSEEK_THINKING"], "disabled")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_DEEPSEEK_REASONING_EFFORT"], "low")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_DEEPSEEK_MAX_TOKENS"], "96")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_PINYIN_FUZZY_ENABLED"], "1")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_PINYIN_FUZZY_PROFILE"], "sichuan-mild")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_PINYIN_FUZZY_S_SH"], "1")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_PINYIN_FUZZY_ONG_ON"], "1")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_PINYIN_FUZZY_N_L"], "0")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_PINYIN_FUZZY_F_H"], "0")
        self.assertTrue(payload["WorkingDirectory"].endswith("RagIme"))
        self.assertIn("sidecar-server", payload["ProgramArguments"])
        self.assertIn("18766", payload["ProgramArguments"])
        self.assertIn("sidecar_launch.py", " ".join(payload["ProgramArguments"]))
        script_source = (root / "scripts" / "install_sidecar_launch_agent.sh").read_text(encoding="utf-8")
        self.assertIn("kill_stale_sidecar_processes", script_source)
        self.assertIn("RAG_IME_KILL_STALE_SIDECAR_ON_INSTALL", script_source)
        self.assertIn("wait_for_sidecar_port_release", script_source)
        self.assertIn('RAG_IME_SIDECAR_HEALTH_TIMEOUT_SECONDS:-45', script_source)
        self.assertIn("while (( SECONDS < health_deadline )); do", script_source)
        self.assertIn('predictor.get("providerProfile")', script_source)
        self.assertIn('predictor.get("promptMode")', script_source)
        self.assertIn('predictor.get("maxTokens")', script_source)
        self.assertIn("expected_predictor_profile", script_source)
        self.assertIn("expected_prompt_mode", script_source)
        self.assertIn("expected_max_tokens", script_source)
        self.assertNotIn("for _attempt in {1..20}", script_source)
        self.assertIn('launchctl kickstart "$DOMAIN/$LABEL"', script_source)
        self.assertNotIn('launchctl kickstart -k "$DOMAIN/$LABEL"', script_source)

    def test_sidecar_installer_resolves_active_hot_model_without_previous_plist(self) -> None:
        root = self._sidecar_checkout()
        with tempfile.TemporaryDirectory(prefix="rag-ime-sidecar-model-registry-") as tmp:
            home = Path(tmp)
            app_support = home / "Library" / "Application Support" / "RagIme"
            model = app_support / "Models" / "minimind-ime-100m"
            model.mkdir(parents=True)
            registry = app_support / "models.json"
            registry.write_text(
                json.dumps(
                    {
                        "schemaVersion": "rag-ime.model-registry.v3",
                        "revision": 1,
                        "models": [
                            {
                                "modelId": "minimind-ime-100m",
                                "path": str(model),
                                "format": "mlx",
                                "fingerprint": "sha256:test",
                                "profile": "minimind_ime_100m_v1",
                                "runtime": "mlx",
                                "endpoint": "http://127.0.0.1:18767",
                                "lane": "hot",
                                "promptMode": "base-completion",
                                "active": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "HOME": str(home),
                "RAG_IME_PYTHON": sys.executable,
                "RAG_IME_LAUNCH_AGENT_DRY_RUN": "1",
            }
            for key in tuple(env):
                if key.startswith("RAG_IME_PREDICTOR_") or key.startswith("RAG_IME_MLX_"):
                    env.pop(key)
            env["RAG_IME_PYTHON"] = sys.executable
            env["RAG_IME_LAUNCH_AGENT_DRY_RUN"] = "1"
            subprocess.run(
                ["bash", str(root / "scripts" / "install_sidecar_launch_agent.sh")],
                cwd=root,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
            plist_path = home / "Library" / "LaunchAgents" / "com.rag-ime.sidecar.plist"
            with plist_path.open("rb") as fh:
                payload = plistlib.load(fh)

        launch_env = payload["EnvironmentVariables"]
        self.assertEqual(launch_env["RAG_IME_PREDICTOR_PROVIDER"], "mlx")
        self.assertEqual(
            launch_env["RAG_IME_PREDICTOR_BASE_URL"],
            "http://127.0.0.1:18767",
        )
        self.assertEqual(launch_env["RAG_IME_PREDICTOR_MODEL"], str(model))
        self.assertEqual(launch_env["RAG_IME_PREDICTOR_PROFILE"], "minimind_ime_v2")
        self.assertEqual(launch_env["RAG_IME_PREDICTOR_PROMPT_MODE"], "base-completion")
        self.assertEqual(launch_env["RAG_IME_PREDICTOR_MAX_TOKENS"], "8")
        self.assertEqual(launch_env["RAG_IME_MODEL_REGISTRY"], str(registry))

    def test_sidecar_install_does_not_publish_marker_before_health(self) -> None:
        root = self._sidecar_checkout()
        reserved_port = self.enterContext(socket.socket())
        reserved_port.bind(("127.0.0.1", 0))
        browser_builder = root / "scripts" / "build_ego_browser_runtime.py"
        browser_builder.write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            "assert len(sys.argv) == 4 and sys.argv[1] == '--output' and sys.argv[3] == '--replace'\n"
            "output = Path(sys.argv[2])\n"
            "output.mkdir(parents=True)\n"
            "(output / 'test-browser-stage.json').write_text(json.dumps(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        with tempfile.TemporaryDirectory(prefix="rag-ime-sidecar-marker-test-") as tmp:
            home = Path(tmp) / "home"
            fake_bin = Path(tmp) / "bin"
            home.mkdir()
            fake_bin.mkdir()
            stubs = Path(tmp) / "stubs"
            stubs.mkdir()
            for module in ("pypdf", "yaml"):
                (stubs / f"{module}.py").write_text("", encoding="utf-8")
            launchctl = fake_bin / "launchctl"
            launchctl_log = Path(tmp) / "launchctl.log"
            launchctl.write_text(
                f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {shlex.quote(str(launchctl_log))}\nexit 0\n",
                encoding="utf-8",
            )
            launchctl.chmod(0o755)
            env = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "PYTHONPATH": str(stubs),
                "RAG_IME_PYTHON": sys.executable,
                "RAG_IME_SIDECAR_PORT": str(reserved_port.getsockname()[1]),
                "RAG_IME_SIDECAR_HEALTH_TIMEOUT_SECONDS": "1",
                "RAG_IME_INSTALL_AGENT_GATEWAY": "0",
                "RAG_IME_KILL_STALE_SIDECAR_ON_INSTALL": "0",
                "RAG_IME_EMBEDDING_PROVIDER": "local-hash",
            }
            result = subprocess.run(
                ["bash", str(root / "scripts" / "install_sidecar_launch_agent.sh")],
                cwd=root,
                env=env,
                check=False,
                text=True,
                capture_output=True,
            )
            marker = (
                home
                / "Library"
                / "Application Support"
                / "RagIme"
                / "app"
                / "rag-ime-install-marker.json"
            )
            marker_exists = marker.exists()
            browser_output = marker.parent / "integrations" / "ego-browser" / "upstream"
            self.assertEqual(
                json.loads((browser_output / "test-browser-stage.json").read_text(encoding="utf-8")),
                ["--output", str(browser_output), "--replace"],
            )
            calls = launchctl_log.read_text(encoding="utf-8")
            self.assertIn("bootstrap", calls)
            self.assertIn("kickstart", calls)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("health: not ready", result.stderr)
        self.assertFalse(marker_exists)

    def test_install_agent_gateway_dry_run_uses_managed_runtime_and_scrubs_source_overrides(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="rag-ime-agent-gateway-launchd-") as tmp:
            home = Path(tmp)
            app_support = home / "Library" / "Application Support" / "RagIme"
            app_code = app_support / "app"
            app_code.mkdir(parents=True)
            shutil.copytree(root / "rag_ime", app_code / "rag_ime")
            shutil.copytree(
                root / "examples" / "vertical_agents",
                app_code / "examples" / "vertical_agents",
            )
            wrapper = app_code / "sidecar_launch.py"
            shutil.copy2(root / "scripts" / "sidecar_launch.py", wrapper)
            (app_code / "rag-ime-install-marker.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": "rag-ime.component-install-marker.v1",
                        "component": "sidecar-runtime",
                        "sourceCommit": "a" * 40,
                    }
                ),
                encoding="utf-8",
            )
            runtime_root = app_support / "PiRuntime"
            runtime_root.mkdir()
            (runtime_root / "current.json").write_text("{}\n", encoding="utf-8")

            launch_agents = home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            side_plist = launch_agents / "com.rag-ime.sidecar.plist"
            with side_plist.open("wb") as handle:
                plistlib.dump(
                    {
                        "ProgramArguments": [sys.executable],
                        "EnvironmentVariables": {
                            "RAG_IME_PI_EXECUTABLE": "/source/pi/dist/cli.js",
                            "RAG_IME_PI_NODE": "/source/node",
                            "RAG_IME_PI_EXTENSION": "/source/rag-ime-control.ts",
                            "RAG_IME_PI_PROTOCOL_VERSION": "1",
                            "RAG_IME_PI_DEBUG_CONTEXT_DIR": "/stale/debug-context",
                            "RAG_IME_PI_DEBUG_CONTEXT_MAX_BYTES": "1073741824",
                            "RAG_IME_DEEPSEEK_MODEL": "deepseek-v4-flash",
                            "RAG_IME_EMBEDDING_WARMUP_DELAY_SECONDS": "60",
                        },
                    },
                    handle,
                )
            env = {
                **os.environ,
                "HOME": str(home),
                "RAG_IME_APP_SUPPORT_DIR": str(app_support),
                "RAG_IME_PYTHON": sys.executable,
                "RAG_IME_LAUNCH_AGENT_DRY_RUN": "1",
                "RAG_IME_REMOTE_ALLOWED_LOGINS": "owner@example.com",
            }
            result = subprocess.run(
                ["bash", str(root / "scripts" / "install_agent_gateway_launch_agent.sh")],
                cwd=root,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
            gateway_plist = launch_agents / "com.rag-ime.agent-gateway.plist"
            with gateway_plist.open("rb") as handle:
                payload = plistlib.load(handle)
            opt_in_directory = home / "private-debug-context"
            opt_in_result = subprocess.run(
                ["bash", str(root / "scripts" / "install_agent_gateway_launch_agent.sh")],
                cwd=root,
                env={
                    **env,
                    "RAG_IME_PI_DEBUG_CONTEXT_DIR": str(opt_in_directory),
                    "RAG_IME_PI_DEBUG_CONTEXT_MAX_BYTES": "65536",
                },
                check=True,
                text=True,
                capture_output=True,
            )
            with gateway_plist.open("rb") as handle:
                opt_in_payload = plistlib.load(handle)

        self.assertIn("dry-run", result.stdout)
        self.assertIn("dry-run", opt_in_result.stdout)
        self.assertEqual(payload["Label"], "com.rag-ime.agent-gateway")
        self.assertIn("agent-gateway", payload["ProgramArguments"])
        launch_env = payload["EnvironmentVariables"]
        self.assertEqual(launch_env["RAG_IME_PI_ENABLED"], "1")
        self.assertNotIn("RAG_IME_PI_VERSION", launch_env)
        self.assertNotIn("RAG_IME_PI_DEBUG_CONTEXT_DIR", launch_env)
        self.assertNotIn("RAG_IME_PI_DEBUG_CONTEXT_MAX_BYTES", launch_env)
        self.assertNotIn("RAG_IME_PI_DEBUG_CONTEXT_MAX_CALLS", launch_env)
        self.assertEqual(launch_env["RAG_IME_AGENT_TOOL_URL"], "http://127.0.0.1:8768/api/agent/tool/execute")
        self.assertEqual(launch_env["RAG_IME_REMOTE_ALLOWED_LOGINS"], "owner@example.com")
        self.assertIn("--web-dist", payload["ProgramArguments"])
        self.assertEqual(
            launch_env["RAG_IME_AGENT_GATEWAY_WEB_DIST"],
            str(app_support / "app" / "control-center-web" / "dist"),
        )
        self.assertEqual(launch_env["RAG_IME_DEEPSEEK_MODEL"], "deepseek-v4-flash")
        self.assertEqual(launch_env["RAG_IME_EMBEDDING_WARMUP_DELAY_SECONDS"], "0")
        self.assertNotIn("RAG_IME_PI_EXECUTABLE", launch_env)
        self.assertNotIn("RAG_IME_PI_NODE", launch_env)
        self.assertNotIn("RAG_IME_PI_EXTENSION", launch_env)
        self.assertNotIn("RAG_IME_PI_PROTOCOL_VERSION", launch_env)
        opt_in_env = opt_in_payload["EnvironmentVariables"]
        self.assertEqual(opt_in_env["RAG_IME_PI_DEBUG_CONTEXT_DIR"], str(opt_in_directory))
        self.assertEqual(opt_in_env["RAG_IME_PI_DEBUG_CONTEXT_MAX_BYTES"], "65536")
        self.assertEqual(opt_in_env["RAG_IME_PI_DEBUG_CONTEXT_MAX_CALLS"], "128")

    def test_install_agent_gateway_rejects_an_installed_runtime_that_differs_from_source(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="rag-ime-agent-gateway-skew-") as tmp:
            home = Path(tmp)
            app_code = home / "Library" / "Application Support" / "RagIme" / "app"
            app_code.mkdir(parents=True)
            shutil.copytree(root / "rag_ime", app_code / "rag_ime")
            (app_code / "rag_ime" / "__init__.py").write_text(
                "# stale installed backend\n",
                encoding="utf-8",
            )
            shutil.copy2(
                root / "scripts" / "sidecar_launch.py",
                app_code / "sidecar_launch.py",
            )
            (app_code / "rag-ime-install-marker.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": "rag-ime.component-install-marker.v1",
                        "component": "sidecar-runtime",
                        "sourceCommit": "a" * 40,
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                ["bash", str(root / "scripts" / "install_agent_gateway_launch_agent.sh")],
                cwd=root,
                env={
                    **os.environ,
                    "HOME": str(home),
                    "RAG_IME_APP_SUPPORT_DIR": str(
                        home / "Library" / "Application Support" / "RagIme"
                    ),
                },
                check=False,
                text=True,
                capture_output=True,
            )
            plist_exists = (
                home
                / "Library"
                / "LaunchAgents"
                / "com.rag-ime.agent-gateway.plist"
            ).exists()

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "installed sidecar code does not match this source checkout",
            result.stderr,
        )
        self.assertFalse(plist_exists)

    def test_web_only_gateway_update_preserves_a_separately_updated_runtime(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="paw-web-only-runtime-") as tmp:
            home = Path(tmp)
            support = home / "support"
            code = support / "app"
            (code / "rag_ime").mkdir(parents=True)
            (code / "examples" / "vertical_agents").mkdir(parents=True)
            backend = code / "rag_ime" / "__init__.py"
            backend.write_text("# separately updated installed runtime\n", encoding="utf-8")
            (code / "sidecar_launch.py").write_text("# installed wrapper\n", encoding="utf-8")
            marker = code / "rag-ime-install-marker.json"
            marker.write_text(json.dumps({"component": "sidecar-runtime", "sourceCommit": "b" * 40}), encoding="utf-8")
            (support / "PiRuntime").mkdir()
            (support / "PiRuntime" / "current.json").write_text("{}\n", encoding="utf-8")
            launch_agents = home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            gateway_plist = launch_agents / "com.rag-ime.agent-gateway.plist"
            with gateway_plist.open("wb") as handle:
                plistlib.dump({"Label": "retained-gateway", "EnvironmentVariables": {}}, handle)
            before = {path: path.read_bytes() for path in (backend, marker, gateway_plist)}
            env = {**os.environ, "HOME": str(home), "RAG_IME_APP_SUPPORT_DIR": str(support),
                   "RAG_IME_PYTHON": sys.executable, "RAG_IME_LAUNCH_AGENT_DRY_RUN": "1"}
            command = ["bash", str(root / "scripts" / "install_agent_gateway_launch_agent.sh"), "--web-only"]
            result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Agent Gateway and Pi unchanged", result.stdout)
            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content)

            marker.write_text("{}\n", encoding="utf-8")
            invalid = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertEqual(gateway_plist.read_bytes(), before[gateway_plist])

    def test_restart_runtime_script_defaults_to_foreground_rag_profile(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script_text = (root / "scripts" / "restart_rag_ime_runtime.sh").read_text(encoding="utf-8")

        self.assertIn('RUNTIME_PROFILE="${RAG_IME_RUNTIME_PROFILE:-foreground-rag-proof}"', script_text)
        self.assertIn("-m rag_ime.runtime_profile --profile", script_text)
        self.assertIn('RAG_IME_POST_COMMIT_COMPLETION_TTL_MS="${RAG_IME_POST_COMMIT_COMPLETION_TTL_MS:-$RAG_IME_PROFILE_POST_COMMIT_COMPLETION_TTL_MS}"', script_text)
        self.assertIn('RAG_IME_POST_COMMIT_MODEL_HARD_TIMEOUT_MS="${RAG_IME_POST_COMMIT_MODEL_HARD_TIMEOUT_MS:-$RAG_IME_PROFILE_POST_COMMIT_MODEL_HARD_TIMEOUT_MS}"', script_text)
        self.assertIn('RAG_IME_POST_COMMIT_MODEL_BUDGET_MS="${RAG_IME_POST_COMMIT_MODEL_BUDGET_MS:-$RAG_IME_PROFILE_POST_COMMIT_MODEL_BUDGET_MS}"', script_text)
        self.assertIn('RAG_IME_SQUIRREL_LATENCY_BUDGET_MS="${RAG_IME_SQUIRREL_LATENCY_BUDGET_MS:-$RAG_IME_PROFILE_SQUIRREL_LATENCY_BUDGET_MS}"', script_text)
        self.assertIn('RAG_IME_SQUIRREL_TIMEOUT_MS="${RAG_IME_SQUIRREL_TIMEOUT_MS:-$RAG_IME_PROFILE_SQUIRREL_TIMEOUT_MS}"', script_text)
        self.assertIn('RAG_IME_LOCAL_MODEL_QUALITY_GATE_MODE="observe"', script_text)
        self.assertIn('if [[ "$RUNTIME_PROFILE" == "foreground-rag-proof" ]]', script_text)
        self.assertIn('FRONTEND_ON_RESTART_DEFAULT="1"', script_text)
        self.assertIn('RAG_IME_ENABLE_FRONTEND_ON_RESTART:-$FRONTEND_ON_RESTART_DEFAULT', script_text)

    def test_predictor_configuration_apply_script_uses_validated_registry_and_both_installers(self) -> None:
        root = Path(__file__).resolve().parents[1]
        apply_script = root / "scripts" / "apply_predictor_configuration.sh"
        script_text = apply_script.read_text(encoding="utf-8")

        self.assertTrue(apply_script.stat().st_mode & 0o111)
        self.assertIn("-m rag_ime.predictor_configuration", script_text)
        self.assertIn("preview --db", script_text)
        self.assertIn("apply --db", script_text)
        self.assertIn("install_mlx_predictor_launch_agent.sh", script_text)
        self.assertIn("install_sidecar_launch_agent.sh", script_text)
        self.assertIn("restored the previous model registry", script_text)
        self.assertIn("unset RAG_IME_MODEL_ID", script_text)

        mlx_installer = (root / "scripts" / "install_mlx_predictor_launch_agent.sh").read_text(encoding="utf-8")
        sidecar_installer = (root / "scripts" / "install_sidecar_launch_agent.sh").read_text(encoding="utf-8")
        self.assertIn('runtime_config = payload.get("runtimeConfig")', mlx_installer)
        self.assertIn('runtime_config.get("temperature")', mlx_installer)
        self.assertIn('runtime_config.get("topP")', mlx_installer)
        self.assertIn('predictor.get("temperature")', sidecar_installer)
        self.assertIn('predictor.get("topP")', sidecar_installer)

        input_apply_script = root / "scripts" / "apply_input_method_configuration.sh"
        input_apply_text = input_apply_script.read_text(encoding="utf-8")
        self.assertTrue(input_apply_script.stat().st_mode & 0o111)
        self.assertIn('! -x "$SQUIRREL_APP/Contents/MacOS/Squirrel"', input_apply_text)
        self.assertIn('RAG_IME_SQUIRREL_DEPLOY=1', input_apply_text)
        self.assertIn('install_squirrel_rag_config.sh', input_apply_text)
        self.assertIn('RAG_IME_DB_PATH="$DB_PATH"', input_apply_text)

    def test_restart_runtime_prefers_present_mlx_bge_q8_without_overriding_explicit_provider(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script_text = (root / "scripts" / "restart_rag_ime_runtime.sh").read_text(encoding="utf-8")

        self.assertIn('PREFERRED_MLX_BGE_Q8_MODEL=', script_text)
        self.assertIn('MLX_BGE_MODE="${RAG_IME_ENABLE_MLX_BGE:-auto}"', script_text)
        self.assertIn('[[ -d "$PREFERRED_MLX_BGE_Q8_MODEL" ]]', script_text)
        self.assertIn('EMBEDDING_PROVIDER_WAS_EXPLICIT=', script_text)
        self.assertIn('RAG_IME_ENABLE_MLX_BGE=1 conflicts with explicit', script_text)
        self.assertIn('MLX BGE model directory does not exist:', script_text)

    def test_install_sidecar_launch_agent_can_explicitly_disable_keepalive(self) -> None:
        root = self._sidecar_checkout()
        with tempfile.TemporaryDirectory(prefix="rag-ime-launchd-keepalive-test-") as tmp:
            env = {
                **os.environ,
                "HOME": tmp,
                "RAG_IME_PYTHON": sys.executable,
                "RAG_IME_LAUNCH_AGENT_DRY_RUN": "1",
                "RAG_IME_LAUNCH_KEEP_ALIVE": "0",
            }
            subprocess.run(
                ["bash", str(root / "scripts" / "install_sidecar_launch_agent.sh")],
                cwd=root,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
            plist_path = Path(tmp) / "Library" / "LaunchAgents" / "com.rag-ime.sidecar.plist"
            with plist_path.open("rb") as fh:
                payload = plistlib.load(fh)

        self.assertFalse(payload["KeepAlive"])

    def test_install_sidecar_launch_agent_dry_run_rejects_broken_python(self) -> None:
        root = self._sidecar_checkout()
        with tempfile.TemporaryDirectory(prefix="rag-ime-launchd-bad-python-test-") as tmp:
            fake_python = Path(tmp) / "python3"
            fake_python.write_text("#!/usr/bin/env bash\nexit 42\n", encoding="utf-8")
            fake_python.chmod(0o755)
            env = {
                **os.environ,
                "HOME": tmp,
                "RAG_IME_PYTHON": str(fake_python),
                "RAG_IME_LAUNCH_AGENT_DRY_RUN": "1",
            }
            result = subprocess.run(
                ["bash", str(root / "scripts" / "install_sidecar_launch_agent.sh")],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("absolute executable Python 3.12+", result.stderr)
        self.assertIn("sqlite3/hashlib/ssl", result.stderr)

    def test_install_sidecar_launch_agent_real_install_requires_project_modules(self) -> None:
        root = self._sidecar_checkout()
        with tempfile.TemporaryDirectory(prefix="rag-ime-launchd-missing-modules-test-") as tmp:
            blockers = Path(tmp) / "blockers"
            blockers.mkdir()
            (blockers / "pypdf.py").write_text(
                "raise ImportError('blocked for preflight test')\n",
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "HOME": tmp,
                "PYTHONPATH": str(blockers),
                "RAG_IME_PYTHON": sys.executable,
                "RAG_IME_KNOWLEDGE_PYTHON": sys.executable,
                "RAG_IME_LAUNCH_AGENT_DRY_RUN": "0",
                "RAG_IME_EMBEDDING_PROVIDER": "local-hash",
            }
            result = subprocess.run(
                ["bash", str(root / "scripts" / "install_sidecar_launch_agent.sh")],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python 3.12+", result.stderr)
        self.assertIn("pypdf/yaml", result.stderr)

    def test_sidecar_python_probe_covers_the_production_import_floor(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts" / "install_sidecar_launch_agent.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("import pypdf", source)
        self.assertIn("import yaml", source)
        self.assertIn("sys.version_info >= (3, 12)", source)
        self.assertLess(
            source.index('$APP_SUPPORT_DIR/KnowledgeRuntime/.venv/bin/python'),
            source.index('$ROOT/.venv/bin/python'),
        )

    def test_install_sidecar_launch_agent_uses_mlx_capable_knowledge_python(self) -> None:
        root = self._sidecar_checkout()
        with tempfile.TemporaryDirectory(prefix="rag-ime-launchd-mlx-python-test-") as tmp:
            home = Path(tmp) / "home"
            plist_path = home / "Library" / "LaunchAgents" / "com.rag-ime.sidecar.plist"
            plist_path.parent.mkdir(parents=True)
            with plist_path.open("wb") as target:
                plistlib.dump(
                    {"EnvironmentVariables": {"RAG_IME_EMBEDDING_PROVIDER": "local-bge-mlx"}},
                    target,
                )

            knowledge_python = (
                home
                / "Library"
                / "Application Support"
                / "RagIme"
                / "KnowledgeRuntime"
                / ".venv"
                / "bin"
                / "python"
            )
            knowledge_python.parent.mkdir(parents=True)
            knowledge_python.symlink_to(sys.executable)
            stubs = Path(tmp) / "stubs"
            for package in ("mlx", "transformers"):
                package_dir = stubs / package
                package_dir.mkdir(parents=True)
                (package_dir / "__init__.py").write_text("", encoding="utf-8")
            for module in ("pypdf", "yaml"):
                (stubs / f"{module}.py").write_text("", encoding="utf-8")

            env = {
                **os.environ,
                "HOME": str(home),
                "PYTHONPATH": str(stubs),
                "RAG_IME_LAUNCH_AGENT_DRY_RUN": "1",
            }
            env.pop("RAG_IME_PYTHON", None)
            subprocess.run(
                ["bash", str(root / "scripts" / "install_sidecar_launch_agent.sh")],
                cwd=root,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
            with plist_path.open("rb") as source:
                payload = plistlib.load(source)

        self.assertEqual(str(knowledge_python), payload["ProgramArguments"][0])

    def test_install_sidecar_launch_agent_rejects_relative_sidecar_python(self) -> None:
        root = self._sidecar_checkout()
        with tempfile.TemporaryDirectory(prefix="rag-ime-launchd-relative-python-") as tmp:
            env = {
                **os.environ,
                "HOME": tmp,
                "RAG_IME_PYTHON": "python3",
                "RAG_IME_LAUNCH_AGENT_DRY_RUN": "1",
            }
            result = subprocess.run(
                ["bash", str(root / "scripts" / "install_sidecar_launch_agent.sh")],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RAG_IME_PYTHON must be an absolute executable file", result.stderr)
        self.assertNotIn("pypdf/yaml", result.stderr)

    def test_install_sidecar_launch_agent_rejects_relative_knowledge_python(self) -> None:
        root = self._sidecar_checkout()
        with tempfile.TemporaryDirectory(prefix="rag-ime-launchd-knowledge-python-") as tmp:
            env = {
                **os.environ,
                "HOME": tmp,
                "RAG_IME_PYTHON": sys.executable,
                "RAG_IME_KNOWLEDGE_PYTHON": "python3",
                "RAG_IME_LAUNCH_AGENT_DRY_RUN": "1",
            }
            result = subprocess.run(
                ["bash", str(root / "scripts" / "install_sidecar_launch_agent.sh")],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be an absolute executable file", result.stderr)
        self.assertNotIn("pypdf/yaml", result.stderr)

    def test_install_sidecar_launch_agent_preserves_existing_knowledge_python(self) -> None:
        root = self._sidecar_checkout()
        with tempfile.TemporaryDirectory(prefix="rag-ime-launchd-preserve-knowledge-python-") as tmp:
            home = Path(tmp) / "home"
            plist_path = home / "Library" / "LaunchAgents" / "com.rag-ime.sidecar.plist"
            plist_path.parent.mkdir(parents=True)
            with plist_path.open("wb") as target:
                plistlib.dump(
                    {"EnvironmentVariables": {"RAG_IME_KNOWLEDGE_PYTHON": sys.executable}},
                    target,
                )
            sidecar_python = Path(tmp) / "sidecar-python"
            sidecar_python.symlink_to(sys.executable)
            stubs = Path(tmp) / "stubs"
            stubs.mkdir()
            for module in ("pypdf", "yaml"):
                (stubs / f"{module}.py").write_text("", encoding="utf-8")
            env = {
                **os.environ,
                "HOME": str(home),
                "PYTHONPATH": str(stubs),
                "RAG_IME_PYTHON": str(sidecar_python),
                "RAG_IME_LAUNCH_AGENT_DRY_RUN": "1",
            }
            subprocess.run(
                ["bash", str(root / "scripts" / "install_sidecar_launch_agent.sh")],
                cwd=root,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
            with plist_path.open("rb") as source:
                payload = plistlib.load(source)

        self.assertEqual(sys.executable, payload["EnvironmentVariables"]["RAG_IME_KNOWLEDGE_PYTHON"])

    def test_install_sidecar_launch_agent_allows_explicit_v1_budget_and_warmup_delay_overrides(self) -> None:
        root = self._sidecar_checkout()
        with tempfile.TemporaryDirectory(prefix="rag-ime-launchd-v1-budget-test-") as tmp:
            env = {
                **os.environ,
                "HOME": tmp,
                "RAG_IME_PYTHON": sys.executable,
                "RAG_IME_LAUNCH_AGENT_DRY_RUN": "1",
                "RAG_IME_POST_COMMIT_MODEL_BUDGET_MS": "1250",
                "RAG_IME_EMBEDDING_WARMUP_DELAY_SECONDS": "17",
            }
            subprocess.run(
                ["bash", str(root / "scripts" / "install_sidecar_launch_agent.sh")],
                cwd=root,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
            plist_path = Path(tmp) / "Library" / "LaunchAgents" / "com.rag-ime.sidecar.plist"
            with plist_path.open("rb") as fh:
                payload = plistlib.load(fh)

        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_POST_COMMIT_MODEL_BUDGET_MS"], "1250")
        self.assertEqual(payload["EnvironmentVariables"]["RAG_IME_EMBEDDING_WARMUP_DELAY_SECONDS"], "17")

    def test_install_sidecar_launch_agent_preserves_predictor_config_but_resets_product_defaults(self) -> None:
        root = self._sidecar_checkout()
        with tempfile.TemporaryDirectory(prefix="rag-ime-launchd-preserve-test-") as tmp:
            home = Path(tmp)
            legacy_extension = home / "legacy-rag-ime-control.ts"
            legacy_extension.write_text("export const staleSecret = 'must-not-be-installed';\n", encoding="utf-8")
            legacy_model_env = home / "legacy-deepseek.env"
            legacy_model_env.write_text(
                "DEEPSEEK_API_KEY=test-only-preserved\n",
                encoding="utf-8",
            )
            managed_skills = (
                home
                / "Library"
                / "Application Support"
                / "RagIme"
                / "Agent"
                / "config"
                / "skills"
            )
            retired_skill = managed_skills / "room-delivery-closure"
            retired_skill.mkdir(parents=True)
            (retired_skill / "SKILL.md").write_text("retired product skill\n", encoding="utf-8")
            custom_skill = managed_skills / "user-custom"
            custom_skill.mkdir()
            (custom_skill / "SKILL.md").write_text("preserve user skill\n", encoding="utf-8")
            plist_path = home / "Library" / "LaunchAgents" / "com.rag-ime.sidecar.plist"
            plist_path.parent.mkdir(parents=True)
            with plist_path.open("wb") as fh:
                plistlib.dump(
                    {
                        "Label": "com.rag-ime.sidecar",
                        "EnvironmentVariables": {
                            "RAG_IME_PREDICTOR_PROVIDER": "mlx",
                            "RAG_IME_PREDICTOR_BASE_URL": "http://127.0.0.1:8767",
                            "RAG_IME_PREDICTOR_MODEL": "/tmp/qwen3.5-0.8b",
                            "RAG_IME_PREDICTOR_PROFILE": "qwen3_06b_ime_hot",
                            "RAG_IME_PREDICTOR_STREAM_FIRST": "1",
                            "RAG_IME_POST_COMMIT_PRESENTATION_STREAM": "1",
                            "RAG_IME_PREDICTOR_ENV": "/tmp/predictor.env",
                            "RAG_IME_PREDICTOR_API_KEY": "preserved-local-secret",
                            "RAG_IME_EMBEDDING_PROVIDER": "local-hash",
                            "RAG_IME_VECTOR_CANDIDATES": "80",
                            "RAG_IME_POST_COMMIT_MODEL_BUDGET_MS": "4500",
                            "RAG_IME_DEEPSEEK_BASE_URL": "https://api.kukuit.com",
                            "RAG_IME_DEEPSEEK_MODEL": "deepseek-v4-flash",
                            "RAG_IME_DEEPSEEK_ENV": str(legacy_model_env),
                            "RAG_IME_DEEPSEEK_ACTIVE_RAG_MAX_TOKENS": "1536",
                            "RAG_IME_PI_EXECUTABLE": "/tmp/pi/dist/cli.js",
                            "RAG_IME_PI_NODE": "/tmp/node",
                            "RAG_IME_PI_EXTENSION": "/tmp/rag-ime-control.ts",
                            "RAG_IME_PI_VERSION": "0.80.2",
                        },
                    },
                    fh,
                )
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("RAG_IME_") and not key.startswith("DEEPSEEK_")
            }
            env.update(
                {
                    "HOME": str(home),
                    "RAG_IME_PYTHON": sys.executable,
                    "RAG_IME_LAUNCH_AGENT_DRY_RUN": "1",
                    "RAG_IME_DEEPSEEK_ENV": str(legacy_model_env),
                    "RAG_IME_PI_EXTENSION": str(legacy_extension),
                }
            )
            result = subprocess.run(
                ["bash", str(root / "scripts" / "install_sidecar_launch_agent.sh")],
                cwd=root,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
            with plist_path.open("rb") as fh:
                payload = plistlib.load(fh)
            managed_extension = (
                home
                / "Library"
                / "Application Support"
                / "RagIme"
                / "app"
                / "integrations"
                / "pi"
                / "rag-ime-control.ts"
            )
            managed_native_session = managed_extension.with_name("pi-native-session.ts")
            managed_extension_text = managed_extension.read_text(encoding="utf-8")
            managed_native_session_text = managed_native_session.read_text(encoding="utf-8")
            managed_memory_skill = (
                home
                / "Library"
                / "Application Support"
                / "RagIme"
                / "Agent"
                / "config"
                / "skills"
                / "memory-curation"
                / "SKILL.md"
            )
            managed_memory_skill_text = managed_memory_skill.read_text(encoding="utf-8")
            retired_skill_exists = retired_skill.exists()
            custom_skill_text = (custom_skill / "SKILL.md").read_text(encoding="utf-8")

        env_vars = payload["EnvironmentVariables"]
        self.assertEqual(env_vars["RAG_IME_PREDICTOR_PROVIDER"], "mlx")
        self.assertEqual(env_vars["RAG_IME_PREDICTOR_BASE_URL"], "http://127.0.0.1:8767")
        self.assertEqual(env_vars["RAG_IME_PREDICTOR_MODEL"], "/tmp/qwen3.5-0.8b")
        self.assertEqual(env_vars["RAG_IME_PREDICTOR_PROFILE"], "qwen3_06b_ime_hot")
        self.assertEqual(env_vars["RAG_IME_PREDICTOR_STREAM_FIRST"], "0")
        self.assertEqual(env_vars["RAG_IME_POST_COMMIT_PRESENTATION_STREAM"], "0")
        self.assertNotIn("RAG_IME_ROOM_KERNEL_MODE", env_vars)
        self.assertEqual(env_vars["RAG_IME_PREDICTOR_ENV"], "/tmp/predictor.env")
        self.assertEqual(env_vars["RAG_IME_PREDICTOR_API_KEY"], "preserved-local-secret")
        self.assertNotIn("preserved-local-secret", result.stdout + result.stderr)
        self.assertEqual(env_vars["RAG_IME_EMBEDDING_PROVIDER"], "local-hash")
        self.assertEqual(env_vars["RAG_IME_VECTOR_CANDIDATES"], "80")
        self.assertEqual(env_vars["RAG_IME_DEEPSEEK_BASE_URL"], "https://api.kukuit.com")
        self.assertEqual(env_vars["RAG_IME_DEEPSEEK_MODEL"], "deepseek-v4-flash")
        self.assertEqual(
            env_vars["RAG_IME_DEEPSEEK_ENV"],
            str(home / "Library" / "Application Support" / "RagIme" / "deepseek.env"),
        )
        self.assertEqual(env_vars["RAG_IME_DEEPSEEK_ACTIVE_RAG_MAX_TOKENS"], "1536")
        self.assertNotIn("RAG_IME_PI_EXECUTABLE", env_vars)
        self.assertNotIn("RAG_IME_PI_NODE", env_vars)
        self.assertNotIn("RAG_IME_PI_EXTENSION", env_vars)
        self.assertEqual(
            managed_extension_text,
            (root / "integrations" / "pi" / "rag-ime-control.ts").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            managed_native_session_text,
            (root / "integrations" / "pi" / "pi-native-session.ts").read_text(encoding="utf-8"),
        )
        self.assertNotIn("must-not-be-installed", managed_extension_text)
        self.assertIn("authorized Evidence -> one Current Atom", managed_memory_skill_text)
        self.assertIn("native review boundary", managed_memory_skill_text)
        self.assertEqual(
            managed_memory_skill_text,
            (
                root
                / "integrations"
                / "pi"
                / "skills"
                / "memory-curation"
                / "SKILL.md"
            ).read_text(encoding="utf-8"),
        )
        self.assertFalse(retired_skill_exists)
        self.assertEqual(custom_skill_text, "preserve user skill\n")
        self.assertNotIn("RAG_IME_PI_VERSION", env_vars)
        self.assertEqual(env_vars["RAG_IME_DEEPSEEK_THINKING"], "disabled")
        self.assertEqual(env_vars["RAG_IME_POST_COMMIT_MODEL_BUDGET_MS"], "900")
        self.assertEqual(env_vars["RAG_IME_ENABLE_POST_COMMIT_AUTO_MODEL"], "1")
        self.assertEqual(env_vars["RAG_IME_RAG_DIRECT_DISPLAY"], "1")

    def test_install_sidecar_launch_agent_does_not_shadow_managed_pi_without_executable(self) -> None:
        root = self._sidecar_checkout()
        with tempfile.TemporaryDirectory(prefix="rag-ime-launchd-managed-pi-test-") as tmp:
            home = Path(tmp)
            stale_extension = home / "stale-rag-ime-control.ts"
            stale_extension.write_text("export const stale = true;\n", encoding="utf-8")
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("RAG_IME_") and not key.startswith("DEEPSEEK_")
            }
            env.update(
                {
                    "HOME": str(home),
                    "RAG_IME_PYTHON": sys.executable,
                    "RAG_IME_LAUNCH_AGENT_DRY_RUN": "1",
                    "RAG_IME_PI_EXTENSION": str(stale_extension),
                }
            )
            subprocess.run(
                ["bash", str(root / "scripts" / "install_sidecar_launch_agent.sh")],
                cwd=root,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
            plist_path = home / "Library" / "LaunchAgents" / "com.rag-ime.sidecar.plist"
            with plist_path.open("rb") as fh:
                payload = plistlib.load(fh)

            managed_extension = (
                home
                / "Library"
                / "Application Support"
                / "RagIme"
                / "app"
                / "integrations"
                / "pi"
                / "rag-ime-control.ts"
            )
            self.assertTrue(managed_extension.is_file())
            self.assertNotIn("RAG_IME_PI_EXTENSION", payload["EnvironmentVariables"])
            self.assertNotIn(str(stale_extension), plistlib.dumps(payload).decode("utf-8"))

    def test_install_sidecar_launch_agent_can_enable_local_vector_baseline(self) -> None:
        root = self._sidecar_checkout()
        with tempfile.TemporaryDirectory(prefix="rag-ime-launchd-vector-test-") as tmp:
            env = {
                **os.environ,
                "HOME": tmp,
                "RAG_IME_PYTHON": sys.executable,
                "RAG_IME_LAUNCH_AGENT_DRY_RUN": "1",
                "RAG_IME_ENABLE_LOCAL_VECTOR": "1",
            }
            for key in list(env):
                if key.startswith("RAG_IME_EMBEDDING_") or key in {
                    "RAG_IME_VECTOR_CANDIDATES",
                    "RAG_IME_VECTOR_WEIGHT",
                    "RAG_IME_VECTOR_AUTO_REBUILD_LIMIT",
                }:
                    env.pop(key, None)
            subprocess.run(
                ["bash", str(root / "scripts" / "install_sidecar_launch_agent.sh")],
                cwd=root,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
            plist_path = Path(tmp) / "Library" / "LaunchAgents" / "com.rag-ime.sidecar.plist"
            with plist_path.open("rb") as fh:
                payload = plistlib.load(fh)

        env_vars = payload["EnvironmentVariables"]
        self.assertEqual(env_vars["RAG_IME_EMBEDDING_PROVIDER"], "local-hash")
        self.assertEqual(env_vars["RAG_IME_EMBEDDING_DIMENSIONS"], "96")
        self.assertEqual(env_vars["RAG_IME_VECTOR_CANDIDATES"], "80")
        self.assertEqual(env_vars["RAG_IME_VECTOR_WEIGHT"], "1.4")
        self.assertEqual(env_vars["RAG_IME_VECTOR_AUTO_REBUILD_LIMIT"], "5000")

    def test_install_mlx_predictor_launch_agent_dry_run_writes_plist(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="rag-ime-mlx-launchd-test-") as tmp:
            env = {
                **os.environ,
                "HOME": tmp,
                "RAG_IME_MLX_PYTHON": sys.executable,
                "RAG_IME_MLX_LAUNCH_AGENT_DRY_RUN": "1",
                "RAG_IME_MLX_PORT": "18767",
                "RAG_IME_MLX_MODEL": "/Users/example/Models/mlx-community-Qwen3.5-0.8B-text-4bit-local",
                "RAG_IME_MLX_PROFILE": "qwen3_06b_ime_hot",
                "RAG_IME_MLX_PROMPT_CACHE": "1",
                "RAG_IME_MLX_PREFIX_CACHE": "0",
                "RAG_IME_MLX_PREFIX_CACHE_MAX_ENTRIES": "8",
                "RAG_IME_MLX_PREFIX_CACHE_MAX_MB": "32",
                "RAG_IME_MLX_PROMPT_MODE": "base-completion",
                "RAG_IME_MEMORY_PROFILE": "low",
                "RAG_IME_HF_HOME": str(Path(tmp) / "hf-cache"),
            }
            result = subprocess.run(
                ["bash", str(root / "scripts" / "install_mlx_predictor_launch_agent.sh")],
                cwd=root,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
            plist_path = Path(tmp) / "Library" / "LaunchAgents" / "com.rag-ime.mlx-predictor.plist"
            app_dir = (
                Path(tmp)
                / "Library"
                / "Application Support"
                / "RagIme"
                / "components"
                / "mlx-predictor"
            )
            self.assertIn(str(plist_path), result.stdout)
            self.assertIn("dry-run", result.stdout)
            self.assertTrue((app_dir / "rag_ime").is_dir())
            self.assertTrue((app_dir / "sidecar_launch.py").is_file())
            marker = json.loads((app_dir / "rag-ime-install-marker.json").read_text(encoding="utf-8"))
            with plist_path.open("rb") as fh:
                payload = plistlib.load(fh)

        self.assertEqual(marker["schemaVersion"], "rag-ime.component-install-marker.v1")
        self.assertEqual(marker["component"], "mlx-predictor")
        self.assertEqual(marker["sourceRoot"], str(root))
        self.assertTrue(marker["sourceCommit"])
        self.assertEqual(marker["pythonExecutable"], sys.executable)
        self.assertEqual(payload["Label"], "com.rag-ime.mlx-predictor")
        self.assertFalse(payload["KeepAlive"])
        self.assertEqual(payload["ProgramArguments"][0], sys.executable)
        self.assertIn("mlx-predictor-server", payload["ProgramArguments"])
        self.assertIn("18767", payload["ProgramArguments"])
        self.assertIn("/Users/example/Models/mlx-community-Qwen3.5-0.8B-text-4bit-local", payload["ProgramArguments"])
        self.assertIn("--profile", payload["ProgramArguments"])
        self.assertIn("qwen3_06b_ime_hot", payload["ProgramArguments"])
        self.assertIn("--prompt-cache", payload["ProgramArguments"])
        env_vars = payload["EnvironmentVariables"]
        self.assertEqual(env_vars["RAG_IME_MLX_MODEL"], "/Users/example/Models/mlx-community-Qwen3.5-0.8B-text-4bit-local")
        self.assertEqual(env_vars["RAG_IME_MLX_PROFILE"], "qwen3_06b_ime_hot")
        self.assertEqual(env_vars["RAG_IME_MLX_PORT"], "18767")
        self.assertEqual(env_vars["RAG_IME_MLX_PROMPT_MODE"], "base-completion")
        self.assertEqual(env_vars["RAG_IME_MLX_PREFIX_CACHE"], "0")
        self.assertEqual(env_vars["RAG_IME_MLX_PREFIX_CACHE_MAX_ENTRIES"], "8")
        self.assertEqual(env_vars["RAG_IME_MLX_PREFIX_CACHE_MAX_MB"], "32")
        self.assertEqual(env_vars["RAG_IME_MEMORY_PROFILE"], "low")
        self.assertEqual(env_vars["HTTP_PROXY"], "")
        self.assertEqual(env_vars["HTTPS_PROXY"], "")
        self.assertEqual(env_vars["ALL_PROXY"], "")
        self.assertEqual(env_vars["HF_HOME"], str(Path(tmp) / "hf-cache"))
        self.assertNotIn("RAG_IME_SOURCE_ROOT", env_vars)
        self.assertTrue(payload["WorkingDirectory"].endswith("RagIme"))
        script_source = (root / "scripts" / "install_mlx_predictor_launch_agent.sh").read_text(encoding="utf-8")
        self.assertIn("kill_stale_mlx_predictor_processes", script_source)
        self.assertIn("wait_for_mlx_port_release", script_source)
        self.assertIn('"$MODEL_FINGERPRINT"', script_source)
        self.assertIn('"$PROFILE"', script_source)
        self.assertIn('"$PROMPT_MODE"', script_source)
        self.assertIn('"$MAX_TOKENS"', script_source)
        self.assertIn('model_profile.get("id")', script_source)
        self.assertIn('payload.get("promptMode")', script_source)
        self.assertIn('model_profile.get("maxTokens")', script_source)
        self.assertIn("runtime_fingerprint = str(payload.get(\"modelFingerprint\")", script_source)
        self.assertIn("matching_sha256", script_source)
        self.assertIn('launchctl kickstart "$DOMAIN/$LABEL"', script_source)
        self.assertNotIn('launchctl kickstart -k "$DOMAIN/$LABEL"', script_source)

    def test_mlx_installer_ignores_checkout_python_from_existing_launch_agent(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="rag-ime-mlx-managed-python-") as tmp:
            home = Path(tmp)
            plist_path = home / "Library" / "LaunchAgents" / "com.rag-ime.mlx-predictor.plist"
            plist_path.parent.mkdir(parents=True)
            checkout_python = home / "source-checkout" / ".venv" / "bin" / "python"
            checkout_python.parent.mkdir(parents=True)
            checkout_python.symlink_to(sys.executable)
            with plist_path.open("wb") as handle:
                plistlib.dump({"ProgramArguments": [str(checkout_python)]}, handle)
            managed_python = (
                home
                / "Library"
                / "Application Support"
                / "RagIme"
                / "components"
                / "mlx-predictor"
                / ".venv"
                / "bin"
                / "python"
            )
            managed_python.parent.mkdir(parents=True)
            managed_python.symlink_to(sys.executable)
            env = {key: value for key, value in os.environ.items() if not key.startswith("RAG_IME_")}
            env.update(
                {
                    "HOME": str(home),
                    "RAG_IME_MLX_LAUNCH_AGENT_DRY_RUN": "1",
                    "RAG_IME_MLX_MODEL": str(home / "model"),
                }
            )

            subprocess.run(
                ["bash", str(root / "scripts" / "install_mlx_predictor_launch_agent.sh")],
                cwd=root,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
            with plist_path.open("rb") as handle:
                payload = plistlib.load(handle)
            marker = json.loads(
                (
                    home
                    / "Library"
                    / "Application Support"
                    / "RagIme"
                    / "components"
                    / "mlx-predictor"
                    / "rag-ime-install-marker.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(payload["ProgramArguments"][0], str(managed_python))
        self.assertEqual(marker["pythonExecutable"], str(managed_python))
        self.assertNotEqual(payload["ProgramArguments"][0], str(checkout_python))
        self.assertNotIn("RAG_IME_SOURCE_ROOT", payload["EnvironmentVariables"])

    def test_mlx_setup_uses_managed_locked_runtime(self) -> None:
        root = Path(__file__).resolve().parents[1]
        setup_source = (root / "scripts" / "setup_mlx_predictor_env.sh").read_text(
            encoding="utf-8"
        )
        restart_source = (root / "scripts" / "restart_rag_ime_runtime.sh").read_text(
            encoding="utf-8"
        )
        overlay = (
            root / "scripts" / "mlx-predictor-overlay-requirements.txt"
        ).read_text(encoding="utf-8")

        self.assertIn('components/mlx-predictor}"', setup_source)
        self.assertIn('VENV_DIR="${RAG_IME_MLX_VENV:-$RUNTIME_ROOT/.venv}"', setup_source)
        self.assertIn("--frozen", setup_source)
        self.assertIn("--extra embedding-mlx", setup_source)
        self.assertIn('"$UV_BIN" pip sync', setup_source)
        self.assertIn("--no-deps", setup_source)
        self.assertIn('"$UV_BIN" pip check', setup_source)
        self.assertLess(
            setup_source.index('"$UV_BIN" pip sync'),
            setup_source.index('"$UV_BIN" pip install'),
        )
        self.assertLess(
            setup_source.index('"$UV_BIN" pip install'),
            setup_source.index('"$UV_BIN" pip check'),
        )
        self.assertNotIn("$ROOT/.venv-mlx", setup_source)
        self.assertNotIn("transformers<5", setup_source)
        self.assertEqual(
            {
                line
                for line in overlay.splitlines()
                if line and not line.startswith("#")
            },
            {
                "mlx-lm==0.31.3",
                "protobuf==7.35.1",
                "sentencepiece==0.2.1",
                "jinja2==3.1.6",
                "markupsafe==3.0.3",
            },
        )
        self.assertIn('MLX_PYTHON="${RAG_IME_MLX_PYTHON:-}"', restart_source)
        self.assertNotIn('MLX_PYTHON="$ROOT/.venv-mlx', restart_source)
        self.assertNotIn('MLX_PYTHON="$ROOT/.venv/bin/python"', restart_source)

    def test_standalone_mlx_installer_infers_qwen_profile_and_model_id(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="rag-ime-mlx-qwen-inference-test-") as tmp:
            home = Path(tmp)
            model = home / "models" / "mlx-community-Qwen3-0.6B-4bit-local"
            model.mkdir(parents=True)
            env = {key: value for key, value in os.environ.items() if not key.startswith("RAG_IME_")}
            env.update(
                {
                    "HOME": str(home),
                    "RAG_IME_MLX_PYTHON": sys.executable,
                    "RAG_IME_MLX_LAUNCH_AGENT_DRY_RUN": "1",
                    "RAG_IME_MLX_MODEL": str(model),
                }
            )

            subprocess.run(
                ["bash", str(root / "scripts" / "install_mlx_predictor_launch_agent.sh")],
                cwd=root,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
            plist_path = home / "Library" / "LaunchAgents" / "com.rag-ime.mlx-predictor.plist"
            with plist_path.open("rb") as handle:
                payload = plistlib.load(handle)

        env_vars = payload["EnvironmentVariables"]
        self.assertEqual(env_vars["RAG_IME_MLX_PROFILE"], "qwen3_06b_ime_hot")
        self.assertEqual(env_vars["RAG_IME_MODEL_ID"], model.name)
        profile_index = payload["ProgramArguments"].index("--profile")
        self.assertEqual(payload["ProgramArguments"][profile_index + 1], "qwen3_06b_ime_hot")

    def test_launch_agent_health_checks_bracket_ipv6_and_disable_proxies(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for script_name in (
            "install_mlx_predictor_launch_agent.sh",
            "install_sidecar_launch_agent.sh",
        ):
            with self.subTest(script=script_name):
                script_source = (root / "scripts" / script_name).read_text(encoding="utf-8")
                self.assertIn(
                    'url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host',
                    script_source,
                )
                self.assertIn(
                    'opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))',
                    script_source,
                )
                self.assertIn(
                    'opener.open(f"http://{url_host}:{port}/health", timeout=1.0)',
                    script_source,
                )

    def test_install_mlx_predictor_launch_agent_rejects_broken_python(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="rag-ime-mlx-bad-python-test-") as tmp:
            fake_python = Path(tmp) / "python3"
            fake_python.write_text("#!/usr/bin/env bash\nexit 42\n", encoding="utf-8")
            fake_python.chmod(0o755)
            env = {
                **os.environ,
                "HOME": tmp,
                "RAG_IME_MLX_PYTHON": str(fake_python),
                "RAG_IME_MLX_LAUNCH_AGENT_DRY_RUN": "1",
            }
            result = subprocess.run(
                ["bash", str(root / "scripts" / "install_mlx_predictor_launch_agent.sh")],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sqlite3/hashlib/ssl", result.stderr)


if __name__ == "__main__":
    unittest.main()
