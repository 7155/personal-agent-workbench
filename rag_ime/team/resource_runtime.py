"""Attach an already selected Team Package snapshot to one Pi Host.

Team Package publication owns source validation and the Team digest.  This
module is the small runtime seam that asks the resident Pi Host to validate,
preview, and install exactly those mounted resources.  It deliberately does
not implement a second package manager or an Agent loop.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from pathlib import Path
from typing import Protocol

from ..agent_runtime_driver import AgentRuntimeError
from .shared_packages import StagedTeamPackage


__all__ = ["TaskPackageInstallReceipt", "install_task_packages"]


_LEGACY_MANIFEST = "rag-ime-plugin.json"
_NATIVE_MANIFEST = "package.json"
_RESOURCE_ROOT = Path("/run/paw/resources")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_PACKAGES = 16
_MAX_TOKEN = 240


class _PackageRuntimeManager(Protocol):
    def plugin_validate(self, source_path: str) -> Mapping[str, object]: ...

    def plugin_prepare_package(self, source: str) -> Mapping[str, object]: ...

    def plugin_preview_install(
        self, payload: Mapping[str, object]
    ) -> Mapping[str, object]: ...

    def plugin_install(self, payload: Mapping[str, object]) -> Mapping[str, object]: ...


class TaskPackageInstallReceipt(dict[str, object]):
    """Bounded JSON-compatible receipt for one task's package attachment.

    A dict keeps this boundary directly serializable by existing runtime
    projections while the constructor centralizes the public shape.
    """

    def __init__(self, packages: Sequence[Mapping[str, object]]) -> None:
        super().__init__(
            {
                "schemaVersion": "rag-ime.team-task-package-install-receipt.v1",
                "ok": True,
                "packages": [dict(package) for package in packages],
            }
        )


def _fail(package: StagedTeamPackage | None, phase: str, cause: BaseException) -> None:
    identity = "team package"
    if package is not None:
        identity = f"team package {package.package_id}@{package.version}"
    # Do not include host exception text: a Runtime Host can mention preview
    # tokens or private paths.  The parent lifecycle will stop the manager on
    # this typed failure and can retain its own diagnostic cause chain.
    raise AgentRuntimeError(f"{identity} {phase} failed") from cause


def _text(value: object, *, label: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AgentRuntimeError(f"team package {label} is invalid")
    if any(ord(char) < 32 for char in value):
        raise AgentRuntimeError(f"team package {label} is invalid")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise AgentRuntimeError(f"team package {label} is invalid")
    return value


def _manifest_identity(
    manifest: object,
    package: StagedTeamPackage,
    *,
    phase: str,
) -> None:
    if not isinstance(manifest, Mapping):
        raise AgentRuntimeError(f"team package {package.package_id}@{package.version} {phase} manifest is invalid")
    # Legacy manifests use id; Pi's native public validation includes both id
    # and name, while simple native fixtures may expose name only.
    if "id" in manifest:
        manifest_id = manifest.get("id")
    else:
        manifest_id = manifest.get("name")
    if manifest_id != package.package_id or manifest.get("version") != package.version:
        raise AgentRuntimeError(
            f"team package {package.package_id}@{package.version} {phase} identity changed"
        )


def _resource_path(package: StagedTeamPackage, index: int) -> str:
    expected = _RESOURCE_ROOT / str(index)
    try:
        actual = Path(package.path)
    except (TypeError, ValueError) as exc:
        raise AgentRuntimeError("team package resource mount is invalid") from exc
    # The server replaces the private staged path with this fixed in-container
    # mount before attaching resources to a Host.  Rejecting every other path
    # prevents a host-side source path from reaching Pi's package handlers.
    if actual != expected:
        raise AgentRuntimeError("team package resource mount must use /run/paw/resources/<index>")
    return str(expected)


def _validate_package(package: object, index: int) -> tuple[StagedTeamPackage, str, str]:
    if not isinstance(package, StagedTeamPackage):
        raise AgentRuntimeError("team package selection contains an invalid package")
    package_id = _text(package.package_id, label="id", maximum=128)
    version = _text(package.version, label="version", maximum=64)
    _digest(package.digest, label="source digest")
    if not isinstance(package.manifest, Mapping):
        raise AgentRuntimeError(f"team package {package_id}@{version} manifest is invalid")
    manifest_name = package.manifest_name
    if manifest_name not in {_LEGACY_MANIFEST, _NATIVE_MANIFEST, ""}:
        raise AgentRuntimeError(f"team package {package_id}@{version} manifest type is invalid")
    if manifest_name == "":
        manifest_name = (
            _NATIVE_MANIFEST
            if "name" in package.manifest and "id" not in package.manifest
            else _LEGACY_MANIFEST
        )
    _manifest_identity(package.manifest, package, phase="selected")
    if not isinstance(package.public_metadata, Mapping):
        raise AgentRuntimeError(f"team package {package_id}@{version} metadata is invalid")
    if (
        package.public_metadata.get("packageId") != package_id
        or package.public_metadata.get("version") != version
    ):
        raise AgentRuntimeError(f"team package {package_id}@{version} metadata identity changed")
    return package, manifest_name, _resource_path(package, index)


def _validation_result(
    value: object,
    package: StagedTeamPackage,
    *,
    phase: str,
) -> tuple[dict[str, object], str]:
    if not isinstance(value, Mapping):
        raise AgentRuntimeError(f"team package {package.package_id}@{package.version} {phase} returned invalid validation")
    result = dict(value)
    _manifest_identity(result.get("manifest"), package, phase=phase)
    runtime_digest = _digest(result.get("digest"), label=f"{phase} runtime digest")
    return result, runtime_digest


def _preview_result(value: object, package: StagedTeamPackage) -> tuple[str, str]:
    if not isinstance(value, Mapping):
        raise AgentRuntimeError(f"team package {package.package_id}@{package.version} preview returned invalid data")
    preview_token = value.get("previewToken")
    payload_sha256 = value.get("payloadSha256")
    if (
        not isinstance(preview_token, str)
        or not preview_token
        or len(preview_token) > _MAX_TOKEN
        or any(ord(char) < 32 for char in preview_token)
    ):
        raise AgentRuntimeError(f"team package {package.package_id}@{package.version} preview token is invalid")
    _digest(payload_sha256, label="preview payload digest")
    return preview_token, str(payload_sha256)


def _installed_plugin(
    value: object,
    package: StagedTeamPackage,
    *,
    expected_runtime_digest: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise AgentRuntimeError(f"team package {package.package_id}@{package.version} install returned invalid data")
    # The current Pi Runtime Host returns the installed plugin directly.  The
    # optional wrapper is accepted for test/adapter hosts that attach a
    # receipt id beside the plugin, but both shapes are verified identically.
    nested = value.get("plugin")
    plugin = dict(nested) if isinstance(nested, Mapping) else dict(value)
    if plugin.get("id") != package.package_id or plugin.get("version") != package.version:
        raise AgentRuntimeError(f"team package {package.package_id}@{package.version} install identity changed")
    runtime_digest = _digest(plugin.get("digest"), label="install runtime digest")
    if runtime_digest != expected_runtime_digest:
        raise AgentRuntimeError(
            f"team package {package.package_id}@{package.version} install digest changed"
        )
    if plugin.get("enabled") is not True:
        raise AgentRuntimeError(f"team package {package.package_id}@{package.version} install is not enabled")
    return {
        "packageId": package.package_id,
        "version": package.version,
        "teamDigest": package.digest,
        "runtimePluginId": str(plugin["id"]),
        "runtimeDigest": runtime_digest,
        "enabled": True,
        "installed": True,
    }


def install_task_packages(
    manager: _PackageRuntimeManager,
    packages: tuple[StagedTeamPackage, ...],
) -> TaskPackageInstallReceipt:
    """Install exactly one Session's mounted Team package tuple.

    ``manager`` is a resident ``PiRuntimeHostManager`` whose config already
    carries the per-attempt approval token.  This function supplies the
    one-time Host preview values and the product's explicit ``apply`` marker;
    it never forwards the approval token or a Team source path itself.
    """

    if not isinstance(packages, tuple):
        raise AgentRuntimeError("team package selection must be a tuple")
    if len(packages) > _MAX_PACKAGES:
        raise AgentRuntimeError("team package selection exceeds its limit")
    validated: list[tuple[StagedTeamPackage, str, str]] = []
    identities: set[str] = set()
    for index, raw_package in enumerate(packages):
        package, manifest_name, resource_path = _validate_package(raw_package, index)
        identity = package.package_id
        if identity in identities:
            raise AgentRuntimeError("team package selection contains duplicate versions")
        identities.add(identity)
        validated.append((package, manifest_name, resource_path))

    required_methods = {"plugin_preview_install", "plugin_install"}
    if any(manifest_name == _NATIVE_MANIFEST for _, manifest_name, _ in validated):
        required_methods.add("plugin_prepare_package")
    if any(manifest_name == _LEGACY_MANIFEST for _, manifest_name, _ in validated):
        required_methods.add("plugin_validate")
    if not all(callable(getattr(manager, name, None)) for name in required_methods):
        raise AgentRuntimeError("Pi Runtime Host does not support package installation")

    receipts: list[dict[str, object]] = []
    for package, manifest_name, resource_path in validated:
        try:
            if manifest_name == _NATIVE_MANIFEST:
                validation_value = manager.plugin_prepare_package(resource_path)
            else:
                validation_value = manager.plugin_validate(resource_path)
        except Exception as exc:
            _fail(
                package,
                "native preparation" if manifest_name == _NATIVE_MANIFEST else "validation",
                exc,
            )
        validation, runtime_digest = _validation_result(
            validation_value,
            package,
            phase="prepare" if manifest_name == _NATIVE_MANIFEST else "validate",
        )

        prepared_package_id = ""
        if manifest_name == _NATIVE_MANIFEST:
            try:
                prepared_package_id = _text(
                    validation.get("preparedPackageId"),
                    label="prepared package id",
                    maximum=_MAX_TOKEN,
                )
            except AgentRuntimeError:
                raise
            if not prepared_package_id:
                raise AgentRuntimeError(
                    f"team package {package.package_id}@{package.version} has no prepared Pi Package id"
                )
        preview_payload: dict[str, object] = {
            "expectedDigest": runtime_digest,
            "enable": True,
        }
        if manifest_name == _NATIVE_MANIFEST:
            preview_payload["preparedPackageId"] = prepared_package_id
        else:
            preview_payload["sourcePath"] = resource_path
        try:
            preview_value = manager.plugin_preview_install(preview_payload)
        except Exception as exc:
            _fail(package, "install preview", exc)
        preview_token, payload_sha256 = _preview_result(preview_value, package)

        install_payload: dict[str, object] = {
            "expectedDigest": runtime_digest,
            "enable": True,
            "previewToken": preview_token,
            "payloadSha256": payload_sha256,
            "confirmText": "apply",
        }
        if manifest_name == _NATIVE_MANIFEST:
            install_payload["preparedPackageId"] = prepared_package_id
        else:
            install_payload["sourcePath"] = resource_path
        try:
            installed = manager.plugin_install(install_payload)
        except Exception as exc:
            _fail(package, "install", exc)
        receipts.append(
            _installed_plugin(
                installed,
                package,
                expected_runtime_digest=runtime_digest,
            )
        )

    return TaskPackageInstallReceipt(receipts)
