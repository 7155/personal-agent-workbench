from __future__ import annotations

import unittest
from pathlib import Path

from rag_ime.agent_runtime_driver import AgentRuntimeError
from rag_ime.team.resource_runtime import install_task_packages
from rag_ime.team.shared_packages import StagedTeamPackage


class _FakePiManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.fail_at: str | None = None
        self.validation: dict[str, dict[str, object]] = {}
        self.installs: dict[str, dict[str, object]] = {}

    def _key(self, path: str) -> str:
        return path.rsplit("/", 1)[-1]

    def plugin_validate(self, source_path: str) -> dict[str, object]:
        self.calls.append(("validate", source_path))
        if self.fail_at == "validate":
            raise ValueError("host validation failed")
        value = self.validation[self._key(source_path)]
        return dict(value)

    def plugin_prepare_package(self, source: str) -> dict[str, object]:
        self.calls.append(("prepare", source))
        if self.fail_at == "prepare":
            raise ValueError("host prepare failed")
        value = self.validation[self._key(source)]
        return dict(value)

    def plugin_preview_install(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(("preview", dict(payload)))
        if self.fail_at == "preview":
            raise ValueError("host preview failed")
        return {
            "previewToken": "preview-token-never-returned",
            "payloadSha256": "c" * 64,
            "requiredConfirm": "apply",
        }

    def plugin_install(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(("install", dict(payload)))
        if self.fail_at == "install":
            raise ValueError("host install failed")
        if "approvalToken" in payload:
            raise AssertionError("manager owns the approval token")
        package_id = str(payload.get("preparedPackageId") or payload.get("sourcePath"))
        package_id = package_id.rsplit("/", 1)[-1]
        result = self.installs[package_id]
        return dict(result)


class TeamResourceRuntimeTests(unittest.TestCase):
    def test_two_versions_of_one_package_are_rejected_before_any_host_call(self) -> None:
        packages = (self._package('legacy', '1.0.0', 0), self._package('legacy', '2.0.0', 1))
        manager = self._manager_for(packages)
        with self.assertRaises(AgentRuntimeError):
            install_task_packages(manager, packages)
        self.assertEqual(manager.calls, [])

    def _package(
        self,
        package_id: str,
        version: str,
        index: int,
        *,
        native: bool = False,
        team_digest: str | None = None,
    ) -> StagedTeamPackage:
        path = Path(f"/run/paw/resources/{index}")
        manifest = (
            {"name": package_id, "version": version}
            if native
            else {"id": package_id, "version": version}
        )
        return StagedTeamPackage(
            package_id=package_id,
            version=version,
            digest=team_digest or ("b" * 64),
            path=path,
            public_metadata={
                "packageId": package_id,
                "version": version,
                "displayName": package_id,
            },
            manifest=manifest,
            manifest_name="package.json" if native else "rag-ime-plugin.json",
        )

    def _manager_for(self, packages: tuple[StagedTeamPackage, ...]) -> _FakePiManager:
        manager = _FakePiManager()
        for package in packages:
            key = package.path.name
            runtime_digest = "a" * 64 if package.package_id == "legacy" else "d" * 64
            manifest = dict(package.manifest)
            manager.validation[key] = {
                "manifest": manifest,
                "digest": runtime_digest,
                "files": [package.manifest_name],
            }
            manager.installs[key] = {
                "id": package.package_id,
                "version": package.version,
                "digest": runtime_digest,
                "enabled": True,
            }
        return manager

    def test_legacy_package_uses_validate_preview_install_and_bounded_receipt(self) -> None:
        package = self._package("legacy", "1.0.0", 0, team_digest="b" * 64)
        manager = self._manager_for((package,))

        receipt = install_task_packages(manager, (package,))

        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["schemaVersion"], "rag-ime.team-task-package-install-receipt.v1")
        item = receipt["packages"][0]
        self.assertEqual(
            item,
            {
                "packageId": "legacy",
                "version": "1.0.0",
                "teamDigest": "b" * 64,
                "runtimePluginId": "legacy",
                "runtimeDigest": "a" * 64,
                "enabled": True,
                "installed": True,
            },
        )
        self.assertEqual([call[0] for call in manager.calls], ["validate", "preview", "install"])
        self.assertEqual(manager.calls[1][1], {
            "sourcePath": "/run/paw/resources/0",
            "expectedDigest": "a" * 64,
            "enable": True,
        })
        install_payload = manager.calls[2][1]
        self.assertEqual(install_payload["confirmText"], "apply")
        self.assertEqual(install_payload["previewToken"], "preview-token-never-returned")
        self.assertEqual(install_payload["payloadSha256"], "c" * 64)
        self.assertEqual(install_payload["expectedDigest"], "a" * 64)
        self.assertNotIn("approvalToken", install_payload)
        self.assertNotIn("previewToken", item)
        self.assertNotIn("payloadSha256", item)
        self.assertNotIn("path", repr(receipt))

    def test_native_package_uses_local_prepare_and_prepared_id_without_source_path(self) -> None:
        package = self._package("native", "2.0.0", 0, native=True)
        manager = self._manager_for((package,))
        manager.validation["0"] = {
            "preparedPackageId": "prepared-native",
            "manifest": dict(package.manifest),
            "digest": "d" * 64,
        }
        manager.installs["prepared-native"] = {
            "id": "native",
            "version": "2.0.0",
            "digest": "d" * 64,
            "enabled": True,
        }

        receipt = install_task_packages(manager, (package,))

        self.assertEqual([call[0] for call in manager.calls], ["prepare", "preview", "install"])
        preview = manager.calls[1][1]
        self.assertEqual(preview["preparedPackageId"], "prepared-native")
        self.assertNotIn("sourcePath", preview)
        install_payload = manager.calls[2][1]
        self.assertEqual(install_payload["preparedPackageId"], "prepared-native")
        self.assertNotIn("sourcePath", install_payload)
        self.assertEqual(receipt["packages"][0]["runtimeDigest"], "d" * 64)

    def test_manifest_identity_mismatch_stops_before_preview(self) -> None:
        package = self._package("legacy", "1.0.0", 0)
        manager = self._manager_for((package,))
        manager.validation["0"]["manifest"] = {"id": "other", "version": "1.0.0"}

        with self.assertRaises(AgentRuntimeError):
            install_task_packages(manager, (package,))
        self.assertEqual([call[0] for call in manager.calls], ["validate"])

    def test_preview_response_is_required_and_install_failure_does_not_continue(self) -> None:
        first = self._package("legacy", "1.0.0", 0)
        second = self._package("second", "1.0.0", 1)
        manager = self._manager_for((first, second))
        manager.fail_at = "install"

        with self.assertRaises(AgentRuntimeError):
            install_task_packages(manager, (first, second))
        self.assertEqual([call[0] for call in manager.calls], ["validate", "preview", "install"])

        manager = self._manager_for((first,))
        manager.fail_at = "preview"
        with self.assertRaises(AgentRuntimeError):
            install_task_packages(manager, (first,))
        self.assertEqual([call[0] for call in manager.calls], ["validate", "preview"])

        manager = self._manager_for((first,))

        def leaked_host_error(_payload: dict[str, object]) -> dict[str, object]:
            raise AgentRuntimeError("preview-token-secret /host/private")

        manager.plugin_preview_install = leaked_host_error  # type: ignore[method-assign]
        with self.assertRaisesRegex(AgentRuntimeError, "install preview failed") as error:
            install_task_packages(manager, (first,))
        self.assertNotIn("preview-token-secret", str(error.exception))
        self.assertNotIn("/host/private", str(error.exception))

    def test_runtime_receipt_identity_digest_and_enabled_state_are_verified(self) -> None:
        package = self._package("legacy", "1.0.0", 0)
        manager = self._manager_for((package,))
        manager.installs["0"] = {
            "id": "legacy",
            "version": "wrong",
            "digest": "a" * 64,
            "enabled": True,
        }
        with self.assertRaises(AgentRuntimeError):
            install_task_packages(manager, (package,))

        manager = self._manager_for((package,))
        manager.installs["0"]["enabled"] = False
        with self.assertRaises(AgentRuntimeError):
            install_task_packages(manager, (package,))

        manager = self._manager_for((package,))
        manager.installs["0"]["digest"] = "c" * 64
        with self.assertRaises(AgentRuntimeError):
            install_task_packages(manager, (package,))

    def test_packages_must_be_unique_fixed_container_paths_and_no_host_path_is_sent(self) -> None:
        package = self._package("legacy", "1.0.0", 0)
        manager = self._manager_for((package,))
        with self.assertRaises(AgentRuntimeError):
            install_task_packages(manager, [package])  # type: ignore[arg-type]
        with self.assertRaises(AgentRuntimeError):
            install_task_packages(manager, (package, package))

        host_path = StagedTeamPackage(
            package_id=package.package_id,
            version=package.version,
            digest=package.digest,
            path=Path("/tmp/private-host-package"),
            public_metadata=package.public_metadata,
            manifest=package.manifest,
            manifest_name=package.manifest_name,
        )
        with self.assertRaises(AgentRuntimeError):
            install_task_packages(manager, (host_path,))
        self.assertEqual(manager.calls, [])


if __name__ == "__main__":
    unittest.main()
