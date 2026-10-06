from __future__ import annotations

import json
import hashlib
import plistlib
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from rag_ime.release_staging import prepare_release_candidate


class ReleaseStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-release-staging-")
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.root, check=True)
        (self.root / "README.md").write_text("public source\n", encoding="utf-8")
        (self.root / "module.py").write_text("print('ok')\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=self.root, check=True)
        self.squirrel = self.root / "squirrel"
        self.squirrel.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.squirrel, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=self.squirrel, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.squirrel, check=True)
        (self.squirrel / "LICENSE.txt").write_text("GPL fixture\n", encoding="utf-8")
        (self.squirrel / "sources").mkdir()
        (self.squirrel / "sources" / "Main.swift").write_text("// source\n", encoding="utf-8")
        (self.squirrel / "download").mkdir()
        (self.squirrel / "download" / "binary.zip").write_bytes(b"not source")
        subprocess.run(["git", "add", "."], cwd=self.squirrel, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=self.squirrel, check=True)
        self.apps = {
            "squirrel": self._app("Squirrel.app", "im.rime.inputmethod.Squirrel"),
            "control": self._app("RagImeControlElectron.app", "com.rag-ime.control"),
            "desktopBridge": self._app(
                "RagImeDesktopBridge.app",
                "com.rag-ime.desktop-bridge",
            ),
            "voice": self._app("RagImeVoice.app", "com.rag-ime.voice"),
        }
        self._mark_electron_web_control(self.apps["control"])
        self._mark_desktop_bridge(self.apps["desktopBridge"])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_staging_is_deterministic_and_cannot_claim_release_ready(self) -> None:
        reports = []
        for name in ("out-a", "out-b"):
            reports.append(
                prepare_release_candidate(
                    self.root,
                    release_id="v0.1.0-rc1",
                    output_root=self.root / name,
                    squirrel_source=self.squirrel,
                    apps=self.apps,
                    project_files=("README.md", "module.py"),
                )
            )

        self.assertFalse(reports[0]["releaseEligible"])
        self.assertEqual(reports[0]["status"], "unsigned_staging_only")
        self.assertNotEqual(reports[0]["schemaVersion"], "rag-ime.release-manifest.v1")
        self.assertEqual(
            [item["sha256"] for item in reports[0]["artifacts"]],
            [item["sha256"] for item in reports[1]["artifacts"]],
        )
        source_archive = Path(reports[0]["artifacts"][0]["path"])
        with tarfile.open(source_archive, "r:gz") as archive:
            names = archive.getnames()
        self.assertIn("patched-squirrel-source/sources/Main.swift", names)
        self.assertNotIn("patched-squirrel-source/download/binary.zip", names)
        self.assertIn("SOURCE-SNAPSHOT.json", names)

    def test_staging_excludes_removed_native_harness_even_if_it_reappears(self) -> None:
        harness = self.root / "macos" / "RagImeMac" / "Info.plist"
        harness.parent.mkdir(parents=True)
        harness.write_text("legacy harness\n", encoding="utf-8")
        installer = self.root / "scripts" / "install_macos_frontend.sh"
        installer.parent.mkdir(parents=True)
        installer.write_text("#!/bin/sh\n", encoding="utf-8")
        obsolete_plan = self.root / "docs" / "codex_next_step_stability_ui_plan.md"
        obsolete_plan.parent.mkdir(parents=True)
        obsolete_plan.write_text("superseded implementation plan\n", encoding="utf-8")

        report = prepare_release_candidate(
            self.root,
            release_id="no-legacy-harness",
            output_root=self.root / "out-no-legacy",
            squirrel_source=self.squirrel,
            apps=self.apps,
            project_files=(
                "README.md",
                "module.py",
                "macos/RagImeMac/Info.plist",
                "scripts/install_macos_frontend.sh",
                "docs/codex_next_step_stability_ui_plan.md",
            ),
        )

        with tarfile.open(Path(report["artifacts"][0]["path"]), "r:gz") as archive:
            names = archive.getnames()
        self.assertNotIn("rag-ime-project/macos/RagImeMac/Info.plist", names)
        self.assertNotIn("rag-ime-project/scripts/install_macos_frontend.sh", names)
        self.assertNotIn("rag-ime-project/docs/codex_next_step_stability_ui_plan.md", names)

    def test_rejects_unsafe_symlink_in_app_bundle(self) -> None:
        bad = self.apps["voice"] / "Contents" / "escape"
        bad.symlink_to("../../../../outside")

        with self.assertRaisesRegex(ValueError, "unsafe symlink"):
            prepare_release_candidate(
                self.root,
                release_id="rc",
                output_root=self.root / "out",
                squirrel_source=self.squirrel,
                apps=self.apps,
                project_files=("README.md",),
            )

    def test_rejects_legacy_native_control_app(self) -> None:
        marker = self.apps["control"] / "Contents/Resources/rag-ime-control-web-build-marker.json"
        marker.unlink()

        with self.assertRaisesRegex(ValueError, "canonical Electron|verified Web Control Center release"):
            prepare_release_candidate(
                self.root,
                release_id="legacy-control",
                output_root=self.root / "out-legacy-control",
                squirrel_source=self.squirrel,
                apps=self.apps,
                project_files=("README.md",),
            )

    def test_accepts_only_canonical_electron_control_layout_and_records_provenance(self) -> None:
        electron_control = self.apps["control"]
        electron_apps = self.apps

        report = prepare_release_candidate(
            self.root,
            release_id="electron-control",
            output_root=self.root / "out-electron-control",
            squirrel_source=self.squirrel,
            apps=electron_apps,
            project_files=("README.md",),
        )

        manifest = json.loads(Path(report["manifest"]).read_text(encoding="utf-8"))
        control_record = next(item for item in manifest["apps"] if item["label"] == "control")
        self.assertEqual(control_record["bundle"], "RagImeControlElectron.app")
        self.assertEqual(
            control_record["provenance"]["frontendProduct"],
            "paw-os",
        )
        self.assertRegex(
            control_record["provenance"]["distTreeDigest"],
            r"^[0-9a-f]{64}$",
        )

        legacy_control = self._app("RagImeControl.app", "com.rag-ime.control")
        self._mark_web_control(legacy_control)
        legacy_apps = {**self.apps, "control": legacy_control}
        with self.assertRaisesRegex(ValueError, "canonical Electron"):
            prepare_release_candidate(
                self.root,
                release_id="legacy-native-layout",
                output_root=self.root / "out-legacy-native-layout",
                squirrel_source=self.squirrel,
                apps=legacy_apps,
                project_files=("README.md",),
            )

    def test_rejects_desktop_bridge_that_declares_screen_capture(self) -> None:
        marker = (
            self.apps["desktopBridge"]
            / "Contents/Resources/rag-ime-desktop-bridge-build-marker.json"
        )
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["capabilities"]["screenCapture"] = True
        marker.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "Accessibility-only"):
            prepare_release_candidate(
                self.root,
                release_id="visual-desktop-bridge",
                output_root=self.root / "out-visual-desktop-bridge",
                squirrel_source=self.squirrel,
                apps=self.apps,
                project_files=("README.md",),
            )

    def test_frontend_provenance_ignores_finder_metadata_but_detects_asset_changes(self) -> None:
        dist = self.apps["control"] / "Contents/Resources/app/dist"
        (dist / ".DS_Store").write_bytes(b"Finder window settings")
        (dist / "assets/.DS_Store").write_bytes(b"Finder icon positions")
        report = prepare_release_candidate(
            self.root, release_id="finder-metadata",
            output_root=self.root / "out-finder-metadata",
            squirrel_source=self.squirrel, apps=self.apps,
            project_files=("README.md",),
        )
        self.assertTrue(Path(report["manifest"]).is_file())
        (dist / "assets/main.js").write_text("unexpected replacement", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "dist tree digest"):
            prepare_release_candidate(
                self.root, release_id="changed-asset",
                output_root=self.root / "out-changed-asset",
                squirrel_source=self.squirrel, apps=self.apps,
                project_files=("README.md",),
            )

    def test_frontend_metadata_name_does_not_hide_a_symlink(self) -> None:
        dist = self.apps["control"] / "Contents/Resources/app/dist"
        (dist / ".DS_Store").symlink_to(dist / "assets/main.js")
        with self.assertRaisesRegex(ValueError, "symlink"):
            prepare_release_candidate(
                self.root, release_id="metadata-symlink",
                output_root=self.root / "out-metadata-symlink",
                squirrel_source=self.squirrel, apps=self.apps,
                project_files=("README.md",),
            )

    def _app(self, name: str, bundle_id: str) -> Path:
        app = self.root / "apps" / name
        contents = app / "Contents"
        executable = contents / "MacOS" / name.removesuffix(".app")
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        with (contents / "Info.plist").open("wb") as handle:
            plistlib.dump({"CFBundleIdentifier": bundle_id}, handle)
        return app

    def _mark_web_control(self, app: Path) -> None:
        resources = app / "Contents" / "Resources"
        frontend = resources / "control-center-web"
        frontend.mkdir(parents=True)
        (resources / "rag-ime-control-web-build-marker.json").write_text(
            json.dumps({
                "bundleId": "com.rag-ime.control",
                "ui": "control-center-web",
                "channel": "release",
                "frontendTransport": "native",
                "frontendBuildChannel": "production",
                "forbiddenTransportModulesExcluded": True,
            }),
            encoding="utf-8",
        )
        (frontend / "rag-ime-control-web-build.json").write_text(
            json.dumps({
                "buildChannel": "production",
                "transport": "native",
                "nativeOnly": True,
                "previewFixturesExcluded": True,
            }),
            encoding="utf-8",
        )

    def _mark_electron_web_control(self, app: Path) -> None:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=self.root,
            text=True,
        ).strip()
        resources = app / "Contents" / "Resources"
        dist = resources / "app" / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text(
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'self'\"><main>paw-os</main>\n",
            encoding="utf-8",
        )
        (dist / "manifest.webmanifest").write_text("{}\n", encoding="utf-8")
        (dist / "assets" / "main.js").write_text("console.log('paw-os');\n", encoding="utf-8")
        (dist / "rag-ime-control-web-build.json").write_text(
            json.dumps(
                {
                    "schemaVersion": "rag-ime.control-web-build.v1",
                    "buildChannel": "production",
                    "transport": "http",
                    "nativeOnly": False,
                    "httpOnly": True,
                    "forbiddenTransportModulesExcluded": True,
                    "previewFixturesExcluded": True,
                    "frontendProduct": "paw-os",
                    "sourceCommit": commit,
                }
            ),
            encoding="utf-8",
        )
        digest_records = []
        for path in sorted(dist.rglob("*"), key=lambda item: item.relative_to(dist).as_posix()):
            if path.is_file() and path.name != "rag-ime-control-web-build.json":
                digest_records.append(
                    {
                        "path": path.relative_to(dist).as_posix(),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
        dist_digest_hash = hashlib.sha256()
        for record in digest_records:
            dist_digest_hash.update(record["path"].encode("utf-8"))
            dist_digest_hash.update(b"\0")
            dist_digest_hash.update((dist / record["path"]).read_bytes())
            dist_digest_hash.update(b"\0")
        dist_digest = dist_digest_hash.hexdigest()
        frontend_marker = json.loads(
            (dist / "rag-ime-control-web-build.json").read_text(encoding="utf-8")
        )
        frontend_marker["distTreeDigest"] = dist_digest
        (dist / "rag-ime-control-web-build.json").write_text(
            json.dumps(frontend_marker),
            encoding="utf-8",
        )
        provenance = {
            "sourceCommit": commit,
            "sourceDirty": False,
            "frontendProduct": "paw-os",
            "bundleId": "com.rag-ime.control",
            "frontendTransport": "http",
            "browserHost": "electron-webview",
            "browserControl": "ego-browser",
            "browserTransport": "cdp",
            "browserPartition": "persist:paw-browser",
            "sameOriginControlProxy": True,
            "distTreeDigest": dist_digest,
        }
        (resources / "rag-ime-control-web-build-marker.json").write_text(
            json.dumps(
                {
                    "schemaVersion": "rag-ime.control-build-marker.v1",
                    "bundleId": "com.rag-ime.control",
                    "gitCommit": commit,
                    "gitDirty": False,
                    "sourceCommit": commit,
                    "sourceDirty": False,
                    "ui": "control-center-web",
                    "channel": "release",
                    "frontendTransport": "http",
                    "frontendBuildChannel": "production",
                    "forbiddenTransportModulesExcluded": True,
                    "browserHost": "electron-webview",
                    "browserControl": "ego-browser",
                    "browserTransport": "cdp",
                    "browserPartition": "persist:paw-browser",
                    "sameOriginControlProxy": True,
                    "frontendProduct": "paw-os",
                    "distTreeDigest": dist_digest,
                    "provenance": provenance,
                }
            ),
            encoding="utf-8",
        )

    def _mark_desktop_bridge(self, app: Path) -> None:
        marker = (
            app
            / "Contents/Resources/rag-ime-desktop-bridge-build-marker.json"
        )
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "schemaVersion": "rag-ime.desktop-bridge-build-marker.v1",
                    "gitCommit": "a" * 40,
                    "gitDirty": False,
                    "capabilities": {
                        "accessibilitySemantics": True,
                        "treeDiff": True,
                        "semanticActions": True,
                        "liveStateRevalidation": True,
                        "modelSuppliedCoordinates": False,
                        "screenCapture": False,
                    },
                }
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
