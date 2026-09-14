from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from rag_ime.agent_extensions import (
    extension_app_binding_capability,
    extension_app_binding_sha256,
)
from rag_ime.team.shared_packages import TeamPackageCatalog, TeamPackageError


class TeamPackageCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="paw-team-packages-")
        self.root = Path(self._temporary.name)
        self.source_root = self.root / "source"
        self.source_root.mkdir()
        self.storage_root = self.root / "storage"
        self.catalog_path = self.source_root / "plugin_catalog.json"
        self._write_plugin(
            "session-review",
            "1.0.0",
            body="export const run = () => 'original';\n",
        )
        self._write_catalog(
            [
                {
                    "id": "session-review",
                    "displayName": "Session Review",
                    "description": "A bounded review action.",
                    "publisher": "PAW",
                    "source": {
                        "kind": "bundled",
                        "label": "Product bundle",
                        "url": "https://example.invalid/public",
                    },
                    "permissions": ["session.read"],
                    "compatibility": {"runtimeProtocol": "2"},
                    "security": {"reviewed": True, "networkAccess": False},
                    "versions": [
                        {
                            "version": "1.0.0",
                            "sourcePath": "bundled_plugins/session-review/1.0.0",
                            "notes": "Initial version",
                        }
                    ],
                },
                {
                    "id": "community-catalog-preview",
                    "displayName": "Community preview",
                    "source": {"kind": "review_only", "label": "Not distributed"},
                    "versions": [],
                },
            ]
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _write_catalog(self, entries: list[dict[str, object]]) -> None:
        self.catalog_path.write_text(
            json.dumps(
                {
                    "schemaVersion": "rag-ime.plugin-catalog.v1",
                    "catalogVersion": "test",
                    "entries": entries,
                }
            ),
            encoding="utf-8",
        )

    def _write_plugin(self, package_id: str, version: str, *, body: str) -> Path:
        package = self.source_root / "bundled_plugins" / package_id / version
        package.mkdir(parents=True, exist_ok=True)
        (package / "rag-ime-plugin.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "id": package_id,
                    "name": "Session Review",
                    "version": version,
                    "description": "A bounded review action.",
                    "entry": "index.ts",
                    "permissions": ["session.read"],
                }
            ),
            encoding="utf-8",
        )
        (package / "index.ts").write_text(body, encoding="utf-8")
        return package

    def _catalog(self) -> TeamPackageCatalog:
        return TeamPackageCatalog(
            self.source_root,
            self.catalog_path,
            self.storage_root,
        )

    def test_list_available_is_bounded_and_hides_source_paths(self) -> None:
        items = self._catalog().list_available()

        self.assertEqual(
            [(item["packageId"], item["version"]) for item in items],
            [("session-review", "1.0.0"), ("community-catalog-preview", None)],
        )
        self.assertTrue(items[0]["installable"])
        self.assertFalse(items[1]["installable"])
        rendered = json.dumps(items)
        self.assertNotIn("sourcePath", rendered)
        self.assertNotIn(str(self.source_root), rendered)
        self.assertNotIn("https://example.invalid", rendered)

    def test_stage_and_load_are_content_addressed_and_source_changes_do_not_drift(self) -> None:
        catalog = self._catalog()
        staged = catalog.stage_install("session-review", "1.0.0")

        self.assertEqual(staged.package_id, "session-review")
        self.assertEqual(staged.version, "1.0.0")
        self.assertRegex(staged.digest, r"^[0-9a-f]{64}$")
        self.assertTrue(staged.path.is_dir())
        self.assertEqual(staged.manifest["id"], "session-review")
        self.assertEqual(
            (staged.path / "index.ts").read_text(encoding="utf-8"),
            "export const run = () => 'original';\n",
        )

        source = self.source_root / "bundled_plugins/session-review/1.0.0/index.ts"
        source.write_text("export const run = () => 'changed';\n", encoding="utf-8")
        loaded = catalog.load(staged.digest)
        self.assertEqual(loaded.digest, staged.digest)
        self.assertEqual(
            (loaded.path / "index.ts").read_text(encoding="utf-8"),
            "export const run = () => 'original';\n",
        )

    def test_real_bundled_session_review_can_stage_and_load_from_read_only_product_source(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        catalog = TeamPackageCatalog(
            repository / "rag_ime",
            repository / "rag_ime" / "plugin_catalog.json",
            self.root / "real-bundle-storage",
        )

        staged = catalog.stage_install("session-review", "1.1.0")
        loaded = catalog.load(staged.digest)
        self.assertEqual(loaded.package_id, "session-review")
        self.assertEqual(loaded.version, "1.1.0")
        self.assertEqual(loaded.manifest_name, "rag-ime-plugin.json")
        self.assertEqual(loaded.public_metadata["manifestName"], "rag-ime-plugin.json")

    def test_same_bytes_can_retry_without_replacing_existing_digest(self) -> None:
        catalog = self._catalog()
        staged = catalog.stage_install("session-review", "1.0.0")

        self.assertEqual(catalog.stage_install("session-review", "1.0.0").digest, staged.digest)
        self.assertEqual(catalog.load(staged.digest).digest, staged.digest)
        self.assertEqual(
            sorted(path.name for path in self.storage_root.iterdir()),
            [staged.digest],
        )
        (self.source_root / 'bundled_plugins/session-review/1.0.0/index.ts').write_text('export default () => "changed";')
        with self.assertRaisesRegex(TeamPackageError, 'different content'):
            catalog.stage_install('session-review', '1.0.0')
        self.assertEqual(catalog.load(staged.digest).digest, staged.digest)

    def test_review_only_and_catalog_traversal_cannot_be_staged(self) -> None:
        catalog = self._catalog()
        with self.assertRaisesRegex(TeamPackageError, "review only"):
            catalog.stage_install("community-catalog-preview", "")

        document = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        document["entries"][0]["versions"][0]["sourcePath"] = "../outside"
        self.catalog_path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(TeamPackageError, "source path"):
            TeamPackageCatalog(
                self.source_root,
                self.catalog_path,
                self.root / "storage-traversal",
            ).stage_install("session-review", "1.0.0")

    def test_source_symlinks_sensitive_files_and_special_files_are_rejected(self) -> None:
        package = self.source_root / "bundled_plugins/session-review/1.0.0"
        (package / "link.ts").symlink_to(package / "index.ts")
        with self.assertRaisesRegex(TeamPackageError, "symbolic link"):
            self._catalog().stage_install("session-review", "1.0.0")
        (package / "link.ts").unlink()

        (package / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        with self.assertRaisesRegex(TeamPackageError, "sensitive"):
            self._catalog().stage_install("session-review", "1.0.0")
        (package / ".env").unlink()

        if hasattr(os, "mkfifo"):
            fifo = package / "pipe"
            os.mkfifo(fifo)
            try:
                with self.assertRaisesRegex(TeamPackageError, "regular"):
                    self._catalog().stage_install("session-review", "1.0.0")
            finally:
                fifo.unlink()

    def test_git_and_node_modules_are_excluded_without_host_side_effects(self) -> None:
        package = self.source_root / "bundled_plugins/session-review/1.0.0"
        (package / ".git").mkdir()
        (package / ".git" / "config").write_text("private", encoding="utf-8")
        (package / "node_modules" / "demo").mkdir(parents=True)
        (package / "node_modules" / "demo" / "index.js").write_text(
            "private", encoding="utf-8"
        )

        staged = self._catalog().stage_install("session-review", "1.0.0")
        self.assertFalse((staged.path / ".git").exists())
        self.assertFalse((staged.path / "node_modules").exists())

    def test_file_and_package_quotas_are_enforced(self) -> None:
        package = self.source_root / "bundled_plugins/session-review/1.0.0"
        (package / "too-large.bin").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(TeamPackageError, "file size"):
            self._catalog().stage_install("session-review", "1.0.0")
        (package / "too-large.bin").unlink()

        for index in range(512):
            (package / f"file-{index:03d}.txt").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(TeamPackageError, "file count"):
            self._catalog().stage_install("session-review", "1.0.0")

    def test_load_detects_stored_file_tampering(self) -> None:
        catalog = self._catalog()
        staged = catalog.stage_install("session-review", "1.0.0")
        (staged.path / "index.ts").write_text("tampered", encoding="utf-8")

        with self.assertRaisesRegex(TeamPackageError, "integrity"):
            catalog.load(staged.digest)

    def test_native_pi_manifest_keeps_verified_extension_app_reference(self) -> None:
        package_id = "@paw/example-app"
        version = "0.2.0"
        package = self.source_root / "native" / "app"
        skill = package / "skills" / "example" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: example\n---\n", encoding="utf-8")
        skill_sha256 = __import__("hashlib").sha256(skill.read_bytes()).hexdigest()
        app_manifest: dict[str, object] = {
            "schemaVersion": "pawos.extension-app.v1",
            "id": "extension:example",
            "version": version,
            "bindingSha256": "0" * 64,
            "packageId": package_id,
            "label": "Example",
            "shortLabel": "Example",
            "tagline": "Example app",
            "route": "/extensions/example",
            "presentation": "conversation",
            "accent": "green",
            "icon": {"symbol": "analytics", "background": "#087F68"},
            "skillRef": "example",
            "skillSha256": skill_sha256,
            "verticalSuiteId": "example-suite",
            "verticalSuiteRevision": "fixture-v1",
        }
        binding_sha256 = extension_app_binding_sha256(
            app_manifest,
            skill_sha256=skill_sha256,
            package_version=version,
        )
        app_manifest["bindingSha256"] = binding_sha256
        binding_capability = extension_app_binding_capability(binding_sha256)
        package.mkdir(parents=True, exist_ok=True)
        (package / "package.json").write_text(
            json.dumps(
                {
                    "name": package_id,
                    "displayName": "Example",
                    "version": version,
                    "pi": {"extensions": ["./index.ts"], "skills": ["./skills"]},
                    "paw": {
                        "capabilities": [binding_capability],
                        "extensionApp": {
                            "id": app_manifest["id"],
                            "packageId": package_id,
                            "version": version,
                            "bindingSha256": binding_sha256,
                            "skillRef": "example",
                            "skillSha256": skill_sha256,
                            "verticalSuiteId": "example-suite",
                            "verticalSuiteRevision": "fixture-v1",
                            "manifest": app_manifest,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        (package / "index.ts").write_text("export {};\n", encoding="utf-8")
        document = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        document["entries"].insert(
            0,
            {
                "id": package_id,
                "displayName": "Example",
                "source": {"kind": "bundled", "label": "Fixture"},
                "versions": [
                    {"version": version, "sourcePath": "native/app"}
                ],
            },
        )
        self.catalog_path.write_text(json.dumps(document), encoding="utf-8")

        staged = self._catalog().stage_install(package_id, version)
        extension = staged.public_metadata.get("extensionApp")
        self.assertIsInstance(extension, dict)
        self.assertEqual(extension["packageId"], package_id)
        self.assertEqual(extension["version"], version)
        self.assertEqual(
            staged.public_metadata["resources"],
            {"extensions": ["index.ts"], "skills": ["skills"]},
        )
        self.assertNotIn("path", extension)

    def test_invalid_manifest_leaves_no_partial_content_addressed_directory(self) -> None:
        package = self.source_root / "bundled_plugins/session-review/1.0.0"
        (package / "rag-ime-plugin.json").write_text("{bad", encoding="utf-8")
        with self.assertRaises(TeamPackageError):
            self._catalog().stage_install("session-review", "1.0.0")
        self.assertTrue(self.storage_root.exists())
        self.assertEqual(list(self.storage_root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
