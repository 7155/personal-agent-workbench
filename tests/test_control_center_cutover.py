from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ControlCenterCutoverTests(unittest.TestCase):
    def stack_fixture(self, root: Path) -> Path:
        """Exercise later preflights on real main, independently of this checkout."""
        scripts = root / "scripts"
        (scripts / "support").mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts/install_product_stack.sh", scripts / "install_product_stack.sh")
        shutil.copyfile(ROOT / "scripts/support/prebuilt_product.sh", scripts / "support/prebuilt_product.sh")
        # Keep the real read-only Pi preflight and its source dependencies.
        (scripts / "build_managed_pi_runtime_v2.py").symlink_to(ROOT / "scripts/build_managed_pi_runtime_v2.py")
        for args in (("init", "-b", "main"), ("add", "."),
                     ("-c", "user.name=PAW test", "-c", "user.email=paw-test@example.invalid",
                      "-c", "commit.gpgsign=false", "commit", "-m", "fixture")):
            subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
        return scripts / "install_product_stack.sh"

    def test_fresh_web_install_authorizes_electron_runtime(self) -> None:
        workspace = (
            ROOT / "control-center-web" / "pnpm-workspace.yaml"
        ).read_text(encoding="utf-8")

        self.assertIn("allowBuilds:\n", workspace)
        self.assertIn("  electron: true\n", workspace)

    def test_web_suite_uses_the_proven_bounded_worker_count(self) -> None:
        package = json.loads(
            (ROOT / "control-center-web" / "package.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(
            package["scripts"]["test"],
            "vitest run --maxWorkers=1 --testTimeout=60000",
        )

    def test_full_stack_installer_only_accepts_the_canonical_main_branch(self) -> None:
        installer = (ROOT / "scripts" / "install_product_stack.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'SOURCE_BRANCH="$(git -C "$ROOT" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"',
            installer,
        )
        self.assertIn('if [[ "$SOURCE_BRANCH" != "main" ]]; then', installer)
        self.assertLess(
            installer.index('if [[ "$SOURCE_BRANCH" != "main" ]]; then'),
            installer.index('echo "Installing product runtime generation $SOURCE_COMMIT"'),
        )

    def test_full_stack_installer_refuses_existing_explicit_squirrel_workspace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-product-stack-") as tmp:
            tmp_path = Path(tmp)
            existing_workdir = tmp_path / "caller-owned-squirrel"
            existing_workdir.mkdir()
            caller_marker = existing_workdir / "keep-me"
            caller_marker.write_text("caller owned\n", encoding="utf-8")
            installer = self.stack_fixture(tmp_path / "source")

            result = subprocess.run(
                ["bash", str(installer), "--include-squirrel"],
                cwd=installer.parent.parent,
                env={
                    **os.environ,
                    "HOME": str(tmp_path),
                    "RAG_IME_ALLOW_DIRTY_INSTALL": "1",
                    "RAG_IME_APP_SUPPORT_DIR": str(tmp_path / "app-support"),
                    "RAG_IME_SQUIRREL_WORKDIR": str(existing_workdir),
                },
                text=True,
                capture_output=True,
            )

            self.assertEqual(result.returncode, 73)
            self.assertIn(
                "refusing to reuse existing explicit RAG_IME_SQUIRREL_WORKDIR",
                result.stderr,
            )
            self.assertEqual(caller_marker.read_text(encoding="utf-8"), "caller owned\n")
            self.assertFalse((tmp_path / "app-support").exists())

    def test_full_stack_installer_preflights_invalid_pi_before_app_support_mutation(self) -> None:
        for case in ("missing", "legacy"):
            with self.subTest(case=case), tempfile.TemporaryDirectory(
                prefix="rag-ime-product-stack-pi-preflight-"
            ) as tmp:
                tmp_path = Path(tmp)
                app_support = tmp_path / "app-support"
                app_support.mkdir()
                sentinel = app_support / "keep-me"
                sentinel.write_text("caller owned\n", encoding="utf-8")
                pi_worktree = tmp_path / "pi"
                if case == "legacy":
                    (pi_worktree / "packages" / "rag-ime-runtime-host").mkdir(
                        parents=True
                    )
                installer = self.stack_fixture(tmp_path / "source")

                result = subprocess.run(
                    [
                        "bash",
                        str(installer),
                        "--include-pi",
                        "--pi-worktree",
                        str(pi_worktree),
                        "--skip-desktop",
                        "--skip-voice",
                        "--skip-maintenance",
                        "--skip-mlx",
                    ],
                    cwd=installer.parent.parent,
                    env={
                        **os.environ,
                        "HOME": str(tmp_path),
                        "RAG_IME_ALLOW_DIRTY_INSTALL": "1",
                        "RAG_IME_APP_SUPPORT_DIR": str(app_support),
                        "RAG_IME_PYTHON": sys.executable,
                    },
                    text=True,
                    capture_output=True,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("managed Pi runtime preflight failed", result.stderr)
                if case == "legacy":
                    self.assertIn("unsupported legacy Pi Runtime Host source", result.stderr)
                else:
                    self.assertIn("expected canonical source", result.stderr)
                self.assertEqual(
                    sorted(path.relative_to(app_support).as_posix() for path in app_support.rglob("*")),
                    ["keep-me"],
                )
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "caller owned\n")

    def test_legacy_swift_control_center_is_removed(self) -> None:
        self.assertFalse((ROOT / "macos" / "RagImeControl").exists())

        tracked = "\n".join(
            path.as_posix()
            for path in ROOT.rglob("*")
            if path.is_file() and "node_modules" not in path.parts and "dist" not in path.parts
        )
        self.assertNotIn("macos/RagImeControl/", tracked)

    def test_public_build_entry_only_dispatches_to_the_electron_host(self) -> None:
        script = (ROOT / "scripts" / "build_control_center.sh").read_text(encoding="utf-8")

        self.assertIn("build_paw_os_electron_host.sh", script)
        self.assertNotIn("build_control_center_web_host.sh", script)
        self.assertIn("build-release", script)
        self.assertIn("install-release", script)
        for forbidden in (
            "native-legacy",
            "RAG_IME_CONTROL_UI",
            "swiftc",
            "SwiftUI",
            "macos/RagImeControl",
        ):
            self.assertNotIn(forbidden, script)

    def test_legacy_webkit_host_source_and_builder_are_removed(self) -> None:
        self.assertFalse((ROOT / "macos" / "RagImeControlWebHost").exists())
        self.assertFalse((ROOT / "scripts" / "build_control_center_web_host.sh").exists())

    def test_web_gate_never_invokes_the_legacy_webkit_host_builder(self) -> None:
        script = (ROOT / "scripts" / "test_control_center_web.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("build_paw_os_electron_host.sh", script)
        self.assertNotIn("build_control_center_web_host.sh", script)

    def test_production_web_build_is_pawos_and_records_a_digest_bound_marker(self) -> None:
        build = (ROOT / "scripts" / "build_control_center_web.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("VITE_PAW_FRONTEND=paw-os", build)
        self.assertIn("frontendProduct", build)
        self.assertIn("distTreeDigest", build)
        self.assertIn("rag-ime-control-web-build.json", build)

    def test_gateway_installs_web_dist_through_a_verified_atomic_cutover(self) -> None:
        installer = (
            ROOT / "scripts" / "install_agent_gateway_launch_agent.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("VITE_PAW_FRONTEND=paw-os", installer)
        self.assertIn("mktemp", installer)
        self.assertIn("distTreeDigest", installer)
        self.assertIn("os.replace", installer)
        self.assertIn("copy", installer)

    def test_web_gates_do_not_invoke_removed_test_modules(self) -> None:
        for script_name in ("test_control_center_web.sh", "run_control_center_web_qa.sh"):
            script = (ROOT / "scripts" / script_name).read_text(encoding="utf-8")
            self.assertNotIn("tests.test_native_control_bridge_contract", script)
            self.assertNotIn("tests.test_control_center_web_host", script)

    def test_pi_model_bundle_only_collects_the_canonical_runtime_host(self) -> None:
        script = (
            ROOT / "scripts" / "build_paw_pi_runtime_model_bundle.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            '"integrations/rag-ime-runtime-host/package.json"',
            script,
        )
        self.assertIn(
            '"integrations/rag-ime-runtime-host/src/**/*.ts"',
            script,
        )
        self.assertNotIn('"packages/rag-ime-runtime-host/package.json"', script)
        self.assertNotIn('"packages/rag-ime-runtime-host/src/**/*.ts"', script)
        self.assertNotIn(
            'pi_root / "packages" / "rag-ime-runtime-host"',
            script,
        )

    def test_release_footprint_gate_requires_the_electron_bundle(self) -> None:
        script = (ROOT / "scripts" / "check_control_center_footprint.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("rag-ime-control-web-build-marker.json", script)
        self.assertIn("Electron Framework.framework", script)
        self.assertIn('WEB_RESOURCES="$APP/Contents/Resources/app/dist"', script)
        self.assertIn('marker.get("frontendTransport") != "http"', script)
        self.assertIn('marker.get("browserHost") != "electron-webview"', script)
        self.assertNotIn("/WebKit.framework/", script)
        self.assertNotIn("native-legacy", script)
        self.assertNotIn("RAG_IME_CONTROL_UI", script)

    def test_full_stack_installer_fails_closed_on_mixed_runtime_generations(self) -> None:
        entry = (ROOT / "scripts" / "build_control_center.sh").read_text(encoding="utf-8")
        installer = (ROOT / "scripts" / "install_product_stack.sh").read_text(encoding="utf-8")

        self.assertIn("install-stack", entry)
        self.assertIn("refusing to install a mixed product stack from dirty tracked source", installer)
        self.assertIn("install_sidecar_launch_agent.sh", installer)
        self.assertIn("install_desktop_bridge_launch_agent.sh", installer)
        self.assertIn("--require desktopBridge", installer)
        self.assertIn("install_agent_gateway_launch_agent.sh", installer)
        self.assertIn("WEB_SUITE_VERIFIED=0", installer)
        self.assertIn("WEB_SUITE_VERIFIED=1", installer)
        self.assertIn('if [[ "$WEB_SUITE_VERIFIED" == "1" ]]', installer)
        self.assertIn("RAG_IME_SKIP_WEB_INSTALL=1", installer)
        self.assertIn("RAG_IME_SKIP_WEB_TESTS=1", installer)
        self.assertIn("build_paw_os_electron_host.sh", installer)
        self.assertNotIn("build_control_center_web_host.sh", installer)
        self.assertLess(
            installer.index("WEB_SUITE_VERIFIED=1"),
            installer.index("RAG_IME_SKIP_WEB_TESTS=1"),
        )
        self.assertIn("build_managed_pi_runtime_v2.py", installer)
        self.assertIn("--pi-worktree", installer)
        self.assertIn("PI_WORKTREE_ARG_SET=0", installer)
        self.assertIn("formal --include-pi requires explicit --pi-worktree", installer)
        self.assertNotIn("../pi", installer)
        self.assertNotIn("../pi/integrations/rag-ime-runtime-host", installer)
        self.assertNotIn("../pi/packages/rag-ime-runtime-host", installer)
        self.assertNotIn("../pi-rag-ime-runtime", installer)
        self.assertIn("install_managed_pi_runtime.py", installer)
        self.assertIn("--no-activate", installer)
        self.assertIn("smoke_pi_session_staged_runtime.py", installer)
        self.assertIn("smoke_pi_room_composition.py", installer)
        self.assertIn("smoke_pi_packages_staged_runtime.py", installer)
        self.assertIn('rm -f -- "$PI_PACKAGE_ACCEPTANCE_REPORT"', installer)
        self.assertIn("--deterministic-test-gate", installer)
        self.assertIn("--acceptance-report", installer)
        self.assertLess(
            installer.index("--no-activate"),
            installer.index("smoke_pi_session_staged_runtime.py"),
        )
        self.assertLess(
            installer.index("smoke_pi_session_staged_runtime.py"),
            installer.index("smoke_pi_room_composition.py"),
        )
        self.assertLess(
            installer.index("smoke_pi_room_composition.py"),
            installer.index("smoke_pi_packages_staged_runtime.py"),
        )
        self.assertLess(
            installer.index('rm -f -- "$PI_PACKAGE_ACCEPTANCE_REPORT"'),
            installer.index("smoke_pi_packages_staged_runtime.py"),
        )
        self.assertLess(
            installer.index("smoke_pi_packages_staged_runtime.py"),
            installer.index("--acceptance-report"),
        )
        self.assertIn("--require piSkills", installer)
        self.assertNotIn("--require roomKernelMode", installer)
        self.assertIn("RAG_IME_INSTALL_AGENT_GATEWAY=0", installer)
        self.assertNotIn("RAG_IME_ROOM_KERNEL_MODE", installer)
        self.assertIn('"$ROOT/scripts/install_sidecar_launch_agent.sh"', installer)
        self.assertLess(
            installer.index('"$ROOT/scripts/install_mlx_predictor_launch_agent.sh"'),
            installer.index('"$ROOT/scripts/install_sidecar_launch_agent.sh"'),
        )
        self.assertIn("install_voice_input_launch_agent.sh", installer)
        self.assertIn("check_installed_product_components.py", installer)
        self.assertIn("--require-current", installer)
        self.assertIn(
            "required=(--require control --require sidecar)",
            installer,
        )
        self.assertNotIn(
            "required=(--require control --require sidecar --require squirrel)",
            installer,
        )
        self.assertIn("required+=(--require squirrel)", installer)
        self.assertIn("prepare_stack_squirrel_workspace()", installer)
        self.assertIn(
            'mktemp -d "${TMPDIR:-/tmp}/rag-ime-squirrel-install-stack.XXXXXX"',
            installer,
        )
        self.assertIn(
            "refusing to reuse existing explicit $label",
            installer,
        )
        self.assertIn(
            'RAG_IME_SQUIRREL_PATCH="$ROOT/squirrel-patches/0001-add-rag-ime-sidecar.patch"',
            installer,
        )
        self.assertIn("RAG_IME_SQUIRREL_RESET=0", installer)
        self.assertIn("RAG_IME_SQUIRREL_BUILD_DRY_RUN=0", installer)
        self.assertIn(
            'SQUIRREL_ALLOW_SOURCE_ROOT_CHANGE="${RAG_IME_SQUIRREL_ALLOW_SOURCE_ROOT_CHANGE:-0}"',
            installer,
        )
        self.assertIn(
            'RAG_IME_SQUIRREL_ALLOW_SOURCE_ROOT_CHANGE="$SQUIRREL_ALLOW_SOURCE_ROOT_CHANGE"',
            installer,
        )
        self.assertNotIn("RAG_IME_SQUIRREL_ALLOW_SOURCE_ROOT_CHANGE=0 \\", installer)
        self.assertIn(
            'run_with_stack_squirrel_workspace "$ROOT/scripts/prepare_squirrel_workspace.sh"',
            installer,
        )
        self.assertIn(
            'run_with_stack_squirrel_workspace "$ROOT/scripts/build_patched_squirrel.sh" install',
            installer,
        )
        self.assertLess(
            installer.rindex("  prepare_stack_squirrel_workspace"),
            installer.index('EXTENSION_SOURCE="$ROOT/integrations/browser-copilot/extension"'),
        )
        self.assertIn("prune_managed_pi_runtime.py", installer)
        self.assertIn("--retain-generations 2", installer)
        self.assertIn("--plan \"$PI_RETENTION_PLAN\"", installer)
        self.assertLess(
            installer.index("check_installed_product_components.py"),
            installer.index("prune_managed_pi_runtime.py"),
        )

        gateway_installer = (
            ROOT / "scripts" / "install_agent_gateway_launch_agent.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("wait_for_gateway_port_release", gateway_installer)
        self.assertIn("bootstrap_launch_agent", gateway_installer)
        self.assertIn('launchctl kickstart "$DOMAIN/$LABEL"', gateway_installer)
        self.assertNotIn('launchctl kickstart -k "$DOMAIN/$LABEL"', gateway_installer)
        self.assertIn("restore_web_source_dist", gateway_installer)
        self.assertIn("trap cleanup_web_install_state EXIT", gateway_installer)
        self.assertIn('ditto "$WEB_SOURCE_DIR/." "$WEB_INSTALL_TEMP"', gateway_installer)

        desktop_installer = (
            ROOT / "scripts" / "install_desktop_bridge_launch_agent.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("wait_for_previous_job_release", desktop_installer)
        self.assertIn("bootstrap_launch_agent", desktop_installer)
        self.assertIn("for attempt in 1 2 3 4 5 6 7 8 9 10", desktop_installer)
        self.assertIn('launchctl print "$DOMAIN/$LABEL"', desktop_installer)
        self.assertNotIn('if launchctl print "$DOMAIN/$LABEL"', desktop_installer)
        self.assertIn('launchctl kickstart "$DOMAIN/$LABEL"', desktop_installer)
        self.assertNotIn('launchctl kickstart -k "$DOMAIN/$LABEL"', desktop_installer)

    def test_electron_release_has_no_swift_fallback(self) -> None:
        build = (ROOT / "scripts" / "build_paw_os_electron_host.sh").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("install_swift_fallback", build)
        self.assertNotIn("RagImeControlWebFallback", build)
        self.assertNotIn("swiftFallback", build)
        self.assertNotIn("build_control_center_web_host.sh", build)
        self.assertIn('"browserHost": "electron-webview"', build)
        self.assertIn('"browserTransport": "cdp"', build)
        self.assertIn('"forbiddenTransportModulesExcluded": True', build)
        self.assertIn('if [[ "$ACTION" == "install-release" ]]; then', build)
        self.assertIn('if [[ "$SOURCE_BRANCH" != "main" ]]', build)
        self.assertIn('if [[ "$SOURCE_DIRTY" == "true" ]]', build)

    def test_electron_host_replaces_atom_identity_and_projects_exact_build(self) -> None:
        build = (ROOT / "scripts" / "build_paw_os_electron_host.sh").read_text(
            encoding="utf-8"
        )
        web_build = (ROOT / "scripts" / "build_control_center_web.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn('build_app_icon.sh" "$RESOURCES/RagImeIcon.icns"', build)
        self.assertIn('rm -f "$RESOURCES/electron.icns"', build)
        self.assertIn('CFBundleIconFile string RagImeIcon', build)
        self.assertIn('CFBundleShortVersionString $PRODUCT_VERSION', build)
        self.assertIn('CFBundleVersion $BUILD_NUMBER', build)
        self.assertIn('RAG_IME_SOURCE_DIRTY="$SOURCE_DIRTY"', build)
        self.assertIn('VITE_PAW_SOURCE_DIRTY="${RAG_IME_SOURCE_DIRTY:-false}"', web_build)

    def test_electron_release_hydrates_the_pinned_runtime(self) -> None:
        build = (ROOT / "scripts" / "build_paw_os_electron_host.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn('ELECTRON_INSTALLER="$WEB/node_modules/electron/install.js"', build)
        self.assertIn("hydrate_electron_runtime()", build)
        self.assertEqual(build.count("hydrate_electron_runtime\n"), 2)
        self.assertIn('node "$ELECTRON_INSTALLER"', build)
        self.assertLess(
            build.index('node "$ELECTRON_INSTALLER"'),
            build.rindex('hydrate_electron_runtime\n'),
        )
        self.assertLess(
            build.index('"$ROOT/scripts/build_control_center_web.sh"'),
            build.rindex('hydrate_electron_runtime\n'),
        )

    def test_database_maintenance_stop_and_reinstall_cover_voice_and_maintenance_jobs(self) -> None:
        stop = (ROOT / "scripts" / "stop_rag_ime_runtime.sh").read_text(encoding="utf-8")
        installer = (ROOT / "scripts" / "install_product_stack.sh").read_text(encoding="utf-8")
        voice = (ROOT / "scripts" / "install_voice_input_launch_agent.sh").read_text(encoding="utf-8")
        maintenance = (
            ROOT / "scripts" / "install_memory_book_maintenance_launch_agent.sh"
        ).read_text(encoding="utf-8")

        for label in ("com.rag-ime.voice", "com.rag-ime.memory-book-maintenance"):
            self.assertIn(label, stop)
        self.assertIn("[R]agImeVoice", stop)
        self.assertIn("[m]emory_book_maintenance_launch.py", stop)
        self.assertIn("install_voice_input_launch_agent.sh", installer)
        self.assertIn("install_memory_book_maintenance_launch_agent.sh", installer)
        self.assertLess(voice.index("launchctl enable"), voice.index("launchctl bootstrap"))
        self.assertLess(maintenance.index("launchctl enable"), maintenance.index("launchctl bootstrap"))

    def test_remote_gateway_uses_tailnet_only_serve_and_loopback_backend(self) -> None:
        script = (
            ROOT / "scripts" / "configure_agent_gateway_tailscale.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("tailscale serve", script.lower())
        self.assertIn("http://127.0.0.1:8768", script)
        self.assertIn("RAG_IME_REMOTE_ALLOWED_LOGINS", script)
        self.assertNotIn('"$TAILSCALE" funnel', script)

    def test_electron_host_owns_the_only_release_bundle(self) -> None:
        build = (ROOT / "scripts" / "build_paw_os_electron_host.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn('INSTALL_DEST="$HOME/Applications/Personal Agent Workbench.app"', build)
        self.assertIn("rag-ime-control-web-build-marker.json", build)
        self.assertIn('"browserHost": "electron-webview"', build)
        self.assertIn('"browserControl": "ego-browser"', build)
        self.assertIn('"browserTransport": "cdp"', build)
        self.assertIn("lsregister", build)
        self.assertNotIn("WebKit.framework", build)
        self.assertNotIn("RagImeControlWebHost", build)

    def test_formal_electron_build_emits_content_addressed_provenance(self) -> None:
        build = (ROOT / "scripts" / "build_paw_os_electron_host.sh").read_text(
            encoding="utf-8"
        )
        footprint = (ROOT / "scripts" / "check_control_center_footprint.sh").read_text(
            encoding="utf-8"
        )
        dist_check = (ROOT / "scripts" / "check_control_center_web_dist.sh").read_text(
            encoding="utf-8"
        )

        for field in (
            '"sourceCommit"',
            '"sourceDirty"',
            '"frontendProduct": "paw-os"',
            '"bundleId"',
            '"frontendTransport": "http"',
            '"browserHost": "electron-webview"',
            '"browserControl": "ego-browser"',
            '"browserTransport": "cdp"',
            '"distTreeDigest"',
        ):
            self.assertIn(field, build)
        self.assertIn('git -C "$ROOT" rev-parse HEAD', footprint)
        self.assertIn("distTreeDigest", footprint)
        self.assertIn("distTreeDigest", dist_check)
        self.assertIn("check_control_center_footprint.sh", build)
        self.assertIn("RAG_IME_CONTROL_APP=", build)

    def test_formal_release_rejects_dirty_source_before_building(self) -> None:
        build = (ROOT / "scripts" / "build_paw_os_electron_host.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('if [[ "$CHANNEL" == "release"', build)
        self.assertIn('&& "$SOURCE_DIRTY" == "true"', build)
        self.assertIn('&& ! ( "$ACTION" == "install-release"', build)
        self.assertIn('"${RAG_IME_ALLOW_DIRTY_INSTALL:-0}" == "1"', build)

        footprint = (
            ROOT / "scripts" / "check_control_center_footprint.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('"${RAG_IME_ALLOW_DIRTY_INSTALL:-0}"', footprint)
        self.assertIn('expected_dirty = allow_dirty == "1"', footprint)


class ElectronDevelopmentInstallTests(unittest.TestCase):
    """Run the real shell admission path, stopping at a harmless build double."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="paw-electron-install-guard-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.case_number = 0
        self.source = (ROOT / "scripts/build_paw_os_electron_host.sh").read_text(encoding="utf-8")

    def fixture(self, branch="main", dirty=False):
        self.case_number += 1
        root = self.tmp / str(self.case_number)
        (root / "scripts/support").mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts/build_paw_os_electron_host.sh", root / "scripts/build_paw_os_electron_host.sh")
        shutil.copyfile(ROOT / "scripts/support/prebuilt_product.sh", root / "scripts/support/prebuilt_product.sh")
        electron = root / "control-center-web/node_modules/electron/dist/Electron.app"
        electron.mkdir(parents=True)
        (electron / "fixture").write_text("pinned runtime fixture\n")
        bins = root / "bin"
        bins.mkdir()
        for path, body in (
            (bins / "node", "printf '1.0.0\\n'\n"),
            (bins / "curl", "exit 0\n"),
            (root / "scripts/build_control_center_web.sh",
             'printf "%s\\n" "$RAG_IME_SOURCE_DIRTY" > "$PAW_TEST_GUARD_LOG"\nexit 73\n'),
        ):
            path.write_text("#!/usr/bin/env bash\nset -eu\n" + body)
            path.chmod(0o755)
        self.git(root, "init", "-b", "main")
        self.git(root, "add", "--force", ".")
        self.git(root, "-c", "user.name=PAW test", "-c", "user.email=paw-test@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
        if branch == "detached":
            self.git(root, "checkout", "--detach")
        elif branch != "main":
            self.git(root, "checkout", "-b", branch)
        if dirty:
            (root / "uncommitted.txt").write_text("keep this local change\n")
        return root

    @staticmethod
    def git(root, *args):
        return subprocess.run(["git", "-C", str(root), *args], check=True, text=True, capture_output=True)

    def run_guard(self, root, action, *, development=None, legacy_dirty=None):
        env = {key: value for key, value in os.environ.items() if key not in {
            "RAG_IME_ALLOW_DEVELOPMENT_INSTALL", "RAG_IME_ALLOW_DIRTY_INSTALL", "PAW_BINARY_PAYLOAD"}}
        env["PATH"] = str(root / "bin") + os.pathsep + env["PATH"]
        log = self.tmp / f"guard-{self.case_number}.txt"
        env["PAW_TEST_GUARD_LOG"] = str(log)
        if development is not None:
            env["RAG_IME_ALLOW_DEVELOPMENT_INSTALL"] = development
        if legacy_dirty is not None:
            env["RAG_IME_ALLOW_DIRTY_INSTALL"] = legacy_dirty
        result = subprocess.run(["bash", str(root / "scripts/build_paw_os_electron_host.sh"), action],
                                cwd=root, env=env, text=True, capture_output=True, timeout=10)
        return result, log

    def test_default_release_actions_require_main_and_clean_source(self):
        for action in ("build-release", "install-release"):
            for branch, dirty in (("codex/feature", False), ("codex/feature", True), ("main", True), ("detached", False)):
                with self.subTest(action=action, branch=branch, dirty=dirty):
                    result, log = self.run_guard(self.fixture(branch, dirty), action)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("refusing formal", result.stderr)
                    self.assertFalse(log.exists())
            with self.subTest(action=action, branch="main", dirty=False):
                result, log = self.run_guard(self.fixture(), action)
                self.assertEqual(result.returncode, 73, result.stderr)
                self.assertEqual(log.read_text().strip(), "false")

    def test_explicit_development_allows_actual_source_without_cleaning_it(self):
        for action in ("build-release", "install-release"):
            for branch, dirty in (("codex/feature", True), ("codex/feature", False), ("main", True), ("detached", True)):
                with self.subTest(action=action, branch=branch, dirty=dirty):
                    root = self.fixture(branch, dirty)
                    before = self.git(root, "status", "--porcelain", "--untracked-files=all").stdout
                    result, log = self.run_guard(root, action, development="1")
                    self.assertEqual(result.returncode, 73, result.stderr)
                    self.assertIn("Development", result.stderr)
                    self.assertEqual(log.read_text().strip(), str(dirty).lower())
                    self.assertEqual(self.git(root, "status", "--porcelain", "--untracked-files=all").stdout, before)

    def test_invalid_or_preview_development_flag_never_reaches_build(self):
        for value in ("true", "yes", "2", "1 "):
            with self.subTest(value=value):
                result, log = self.run_guard(self.fixture(), "install-release", development=value)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("RAG_IME_ALLOW_DEVELOPMENT_INSTALL", result.stderr)
                self.assertFalse(log.exists())
        for action in ("build", "install-preview"):
            with self.subTest(action=action):
                result, log = self.run_guard(self.fixture("codex/feature", True), action, development="1")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(log.exists())

    def test_legacy_dirty_opt_in_stays_limited_to_main_install(self):
        result, log = self.run_guard(self.fixture("main", True), "install-release", legacy_dirty="1")
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertEqual(log.read_text().strip(), "true")
        for branch, action in (("codex/feature", "install-release"), ("main", "build-release")):
            with self.subTest(branch=branch, action=action):
                result, log = self.run_guard(self.fixture(branch, True), action, legacy_dirty="1")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertFalse(log.exists())

    def test_build_marker_records_branch_dirty_and_development_independently(self):
        marker_command = self.source.split('python3 - "$RESOURCES/rag-ime-control-web-build-marker.json"', 1)[1]
        marker_python = marker_command.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        for branch, dirty, development in (("main", "false", "false"), ("codex/feature", "false", "true"),
                                           ("codex/feature", "true", "true"), ("", "true", "true")):
            with self.subTest(branch=branch, dirty=dirty):
                target = self.tmp / "marker.json"
                result = subprocess.run([sys.executable, "-", str(target), "com.rag-ime.control", "release", "production",
                    "a" * 40, dirty, "b" * 64, "1.0.0", "7", branch, development],
                    input=marker_python, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                marker = json.loads(target.read_text())
                self.assertEqual(marker["sourceBranch"], branch)
                self.assertIs(marker["sourceDirty"], dirty == "true")
                self.assertIs(marker["gitDirty"], dirty == "true")
                self.assertIs(marker["developmentInstall"], development == "true")
                self.assertIs(marker["provenance"]["sourceDirty"], dirty == "true")

    def test_footprint_check_receives_observed_dirty_state_instead_of_opt_in(self):
        body = self.source.split("verify_release_provenance() {\n", 1)[1].split("\n}\n", 1)[0]
        scripts = self.tmp / "scripts"
        scripts.mkdir()
        check = scripts / "check_control_center_footprint.sh"
        check.write_text('#!/usr/bin/env bash\nprintf "%s" "$RAG_IME_ALLOW_DIRTY_INSTALL"\n')
        check.chmod(0o755)
        # Remove only the output redirect so the real function's checker env
        # is observable without starting a bundle or invoking the actual gate.
        body = body.replace(">/dev/null", "")
        for dirty in ("true", "false"):
            result = subprocess.run(["bash", "-c", "set -eu\nverify_release_provenance() {\n" + body +
                '\n}\nverify_release_provenance "fixture.app"\n'],
                env={**os.environ, "ROOT": str(self.tmp), "SOURCE_COMMIT": "a" * 40,
                     "SOURCE_DIRTY": dirty, "RAG_IME_ALLOW_DIRTY_INSTALL": "1"}, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "1" if dirty == "true" else "0")


if __name__ == "__main__":
    unittest.main()
