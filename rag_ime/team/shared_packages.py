"""Read-only, content-addressed Team sources for PAW Packages.

The Team package catalog deliberately stops at preparing an immutable source
tree.  Pi owns package loading and execution; this module never imports a Pi
host, evaluates JavaScript, runs an installer, resolves dependencies, or
contacts a provider/network.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import shutil
import tempfile
from threading import RLock


CATALOG_SCHEMA = "rag-ime.plugin-catalog.v1"
PACKAGE_MANIFEST = "rag-ime-plugin.json"
NATIVE_MANIFEST = "package.json"
_META_NAME = ".team-package-meta.json"
_META_SCHEMA_VERSION = 1

MAX_CATALOG_BYTES = 2 * 1024 * 1024
MAX_CATALOG_ENTRIES = 512
MAX_PACKAGE_FILES = 512
MAX_PACKAGE_FILE_BYTES = 2 * 1024 * 1024
MAX_PACKAGE_BYTES = 16 * 1024 * 1024
MAX_METADATA_BYTES = 256 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9@._/-]{1,128}$")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]{0,63}$")
_IGNORED_DIRECTORIES = frozenset({".git", "node_modules"})
_SENSITIVE_EXACT = frozenset(
    {
        "auth",
        "auth.json",
        "credentials",
        "credentials.json",
        "secret",
        "secret.json",
        "secrets",
        "secrets.json",
        "models.json",
        "token",
        "tokens",
        "id_rsa",
        "id_ed25519",
    }
)
_SENSITIVE_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx"})
_PUBLIC_PATH_KEYS = frozenset(
    {
        "path",
        "sourcePath",
        "sourceRoot",
        "hostPath",
        "storagePath",
        "catalogPath",
    }
)


class TeamPackageError(ValueError):
    """A safe, user-facing error from catalog validation or package staging."""

    def __init__(self, message: str, *, code: str = "invalid_package") -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class StagedTeamPackage:
    """A fixed package source prepared for a later Session snapshot.

    ``path`` is an internal controller path.  It is intentionally absent from
    ``public_metadata`` and must not be sent to Team API consumers.
    """

    package_id: str
    version: str
    digest: str
    path: Path
    public_metadata: dict[str, object]
    manifest: dict[str, object]
    manifest_name: str = ""
    extension_app: dict[str, object] | None = None


@dataclass(frozen=True)
class _SnapshotFile:
    relative_path: str
    data: bytes
    executable: bool


@dataclass(frozen=True)
class _CatalogRow:
    entry: dict[str, object]
    version_record: dict[str, object] | None


def _absolute_path(value: Path, *, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise TeamPackageError(
            f"{label} must be an absolute normalized path",
            code="trusted_root_invalid",
        )
    return path


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_stable_system_alias(path: Path, info: os.stat_result) -> bool:
    if not stat.S_ISLNK(info.st_mode):
        return False
    try:
        target = os.readlink(path)
    except OSError:
        return False
    return (path == Path("/var") and target == "private/var") or (
        path == Path("/tmp") and target == "private/tmp"
    )


def _assert_no_symlink_path(path: Path, *, label: str, allow_missing: bool = False) -> None:
    """Check every existing component without resolving a symlink."""

    current = Path(path.anchor or "/")
    parts = path.parts[1:] if path.anchor else path.parts
    missing = False
    for part in parts:
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if allow_missing:
                missing = True
                continue
            raise TeamPackageError(
                f"{label} does not exist",
                code="trusted_root_missing",
            ) from None
        except OSError as exc:
            raise TeamPackageError(
                f"{label} cannot be inspected",
                code="trusted_root_unavailable",
            ) from exc
        if missing and not allow_missing:
            raise TeamPackageError(
                f"{label} does not exist",
                code="trusted_root_missing",
            )
        # macOS exposes the normal temporary roots through these stable
        # system aliases.  They are part of the trusted platform path, while
        # package/source components below remain strict O_NOFOLLOW paths.
        stable_system_alias = _is_stable_system_alias(current, info)
        if stat.S_ISLNK(info.st_mode) and not stable_system_alias:
            raise TeamPackageError(
                f"{label} contains a symbolic link",
                code="trusted_root_symlink",
            )


def _ensure_directory(path: Path, *, label: str) -> None:
    """Create a controlled directory while rejecting symlink components."""

    _assert_no_symlink_path(path, label=label, allow_missing=True)
    current = Path(path.anchor or "/")
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            try:
                os.mkdir(current, 0o700)
            except FileExistsError:
                # A concurrent creator is acceptable only after its type is
                # checked below; a concurrent symlink is never accepted.
                pass
            info = os.lstat(current)
        if (stat.S_ISLNK(info.st_mode) and not _is_stable_system_alias(current, info)) or not (
            stat.S_ISDIR(info.st_mode) or _is_stable_system_alias(current, info)
        ):
            raise TeamPackageError(
                f"{label} must contain directories only",
                code="trusted_root_invalid",
            )


def _read_regular(path: Path, *, maximum: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise TeamPackageError(
            f"{label} cannot be opened safely",
            code="file_unavailable",
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise TeamPackageError(
                f"{label} must be a regular file",
                code="special_file",
            )
        if info.st_size > maximum:
            raise TeamPackageError(
                f"{label} exceeds its file size limit",
                code="file_size_limit",
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise TeamPackageError(
                    f"{label} exceeds its file size limit",
                    code="file_size_limit",
                )
        after = os.fstat(descriptor)
        identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity != after_identity or len(b"".join(chunks)) != after.st_size:
            raise TeamPackageError(
                f"{label} changed while it was being read",
                code="source_changed",
            )
        return b"".join(chunks)
    except TeamPackageError:
        raise
    except OSError as exc:
        raise TeamPackageError(
            f"{label} cannot be read safely",
            code="file_unavailable",
        ) from exc
    finally:
        os.close(descriptor)


def _read_fd_file(
    directory_fd: int,
    name: str,
    *,
    maximum: int,
    label: str,
) -> tuple[bytes, bool]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise TeamPackageError(
            f"{label} cannot be opened safely",
            code="file_unavailable",
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise TeamPackageError(
                f"{label} must be a regular file",
                code="special_file",
            )
        if info.st_size > maximum:
            raise TeamPackageError(
                f"{label} exceeds its file size limit",
                code="file_size_limit",
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise TeamPackageError(
                    f"{label} exceeds its file size limit",
                    code="file_size_limit",
                )
        after = os.fstat(descriptor)
        identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        data = b"".join(chunks)
        if identity != after_identity or len(data) != after.st_size:
            raise TeamPackageError(
                f"{label} changed while it was being read",
                code="source_changed",
            )
        return data, bool(info.st_mode & stat.S_IXUSR)
    except TeamPackageError:
        raise
    except OSError as exc:
        raise TeamPackageError(
            f"{label} cannot be read safely",
            code="file_unavailable",
        ) from exc
    finally:
        os.close(descriptor)


def _safe_segment(name: str, *, label: str) -> str:
    if (
        not name
        or name in {".", ".."}
        or "\x00" in name
        or "/" in name
        or "\\" in name
        or any(ord(char) < 32 for char in name)
        or len(name) > 255
    ):
        raise TeamPackageError(
            f"{label} contains an invalid path segment",
            code="path_invalid",
        )
    return name


def _safe_relative(value: object, *, label: str, allow_dot_prefix: bool = False) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise TeamPackageError(
            f"{label} is invalid",
            code="path_invalid",
        )
    if value.startswith("/") or "\\" in value or any(char in value for char in ":?#"):
        raise TeamPackageError(
            f"{label} must be a relative path",
            code="path_invalid",
        )
    raw_parts = value.split("/")
    if allow_dot_prefix and raw_parts and raw_parts[0] == ".":
        raw_parts = raw_parts[1:]
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        raise TeamPackageError(
            f"{label} contains traversal or empty segments",
            code="path_invalid",
        )
    return tuple(_safe_segment(part, label=label) for part in raw_parts)


def _safe_identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise TeamPackageError(
            f"{label} is invalid",
            code="identifier_invalid",
        )
    if value.startswith("/") or value.endswith("/") or "//" in value:
        raise TeamPackageError(
            f"{label} is invalid",
            code="identifier_invalid",
        )
    if any(part in {".", ".."} for part in value.split("/")):
        raise TeamPackageError(
            f"{label} contains traversal",
            code="identifier_invalid",
        )
    return value


def _safe_version(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_VERSION.fullmatch(value):
        raise TeamPackageError(
            f"{label} is invalid",
            code="version_invalid",
        )
    return value


def _is_sensitive_name(name: str) -> bool:
    lowered = name.casefold()
    if lowered.startswith(".env") or lowered in _SENSITIVE_EXACT:
        return True
    if lowered.endswith(tuple(_SENSITIVE_SUFFIXES)):
        return True
    return lowered.startswith(
        ("auth.", "credential.", "credentials.", "secret.", "secrets.", "token.", "tokens.")
    )


def _normalize_manifest_reference(value: object, *, label: str) -> str:
    parts = _safe_relative(value, label=label, allow_dot_prefix=True)
    return "/".join(parts)


def _has_path(files: Mapping[str, _SnapshotFile], reference: str) -> bool:
    if reference in files:
        return True
    prefix = reference + "/"
    return any(path.startswith(prefix) for path in files)


def _package_digest(files: Sequence[_SnapshotFile]) -> str:
    """Match the existing native Pi digest: sorted path + bytes framing."""

    digest = hashlib.sha256()
    for item in sorted(
        files,
        key=lambda value: tuple(
            (part.casefold(), part) for part in value.relative_path.split("/")
        ),
    ):
        digest.update(item.relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.data)
        digest.update(b"\0")
    return digest.hexdigest()


def _json_object(data: bytes, *, label: str, maximum: int) -> dict[str, object]:
    if len(data) > maximum:
        raise TeamPackageError(
            f"{label} exceeds its JSON size limit",
            code="json_size_limit",
        )
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TeamPackageError(
            f"{label} is not valid UTF-8 JSON",
            code="manifest_invalid",
        ) from exc
    if not isinstance(value, dict):
        raise TeamPackageError(
            f"{label} must contain a JSON object",
            code="manifest_invalid",
        )
    return dict(value)


def _required_text(value: object, *, label: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise TeamPackageError(
            f"{label} is invalid",
            code="manifest_invalid",
        )
    return value.strip()


def _validate_manifest(
    files: Sequence[_SnapshotFile],
    *,
    package_id: str,
    version: str,
) -> tuple[str, dict[str, object], bool]:
    by_path = {item.relative_path: item for item in files}
    candidates = [
        name for name in (PACKAGE_MANIFEST, NATIVE_MANIFEST) if name in by_path
    ]
    if len(candidates) != 1:
        raise TeamPackageError(
            "package must contain exactly one supported root manifest",
            code="manifest_invalid",
        )
    manifest_name = candidates[0]
    manifest = _json_object(
        by_path[manifest_name].data,
        label=manifest_name,
        maximum=MAX_PACKAGE_FILE_BYTES,
    )
    if manifest_name == PACKAGE_MANIFEST:
        schema_version = manifest.get("schemaVersion")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != 1
        ):
            raise TeamPackageError(
                "rag-ime-plugin.json schemaVersion must be 1",
                code="manifest_invalid",
            )
        manifest_id = _required_text(manifest.get("id"), label="plugin manifest id")
        if manifest_id != package_id:
            raise TeamPackageError(
                "plugin manifest id does not match catalog package",
                code="manifest_mismatch",
            )
        manifest_version = _safe_version(
            manifest.get("version"), label="plugin manifest version"
        )
        if manifest_version != version:
            raise TeamPackageError(
                "plugin manifest version does not match catalog version",
                code="manifest_mismatch",
            )
        _required_text(manifest.get("name"), label="plugin manifest name")
        entry = _normalize_manifest_reference(
            manifest.get("entry"), label="plugin manifest entry"
        )
        if entry not in by_path:
            raise TeamPackageError(
                "plugin manifest entry does not exist",
                code="manifest_invalid",
            )
        permissions = manifest.get("permissions", [])
        if not isinstance(permissions, list) or any(
            not isinstance(item, str) or not item.strip() for item in permissions
        ):
            raise TeamPackageError(
                "plugin manifest permissions must be strings",
                code="manifest_invalid",
            )
        return manifest_name, manifest, False

    manifest_name_value = _required_text(
        manifest.get("name"), label="Pi package name"
    )
    if manifest_name_value != package_id:
        raise TeamPackageError(
            "Pi package name does not match catalog package",
            code="manifest_mismatch",
        )
    manifest_version = _safe_version(
        manifest.get("version"), label="Pi package version"
    )
    if manifest_version != version:
        raise TeamPackageError(
            "Pi package version does not match catalog version",
            code="manifest_mismatch",
        )
    pi = manifest.get("pi")
    if pi is not None:
        if not isinstance(pi, Mapping):
            raise TeamPackageError(
                "Pi package pi field must be an object",
                code="manifest_invalid",
            )
        for resource_kind in ("extensions", "skills", "prompts", "themes"):
            values = pi.get(resource_kind)
            if values is None:
                continue
            if not isinstance(values, list):
                raise TeamPackageError(
                    f"Pi package {resource_kind} must be a list",
                    code="manifest_invalid",
                )
            for value in values:
                reference = _normalize_manifest_reference(
                    value,
                    label=f"Pi package {resource_kind} reference",
                )
                if not _has_path(by_path, reference):
                    raise TeamPackageError(
                        f"Pi package {resource_kind} reference does not exist",
                        code="manifest_invalid",
                    )
    paw = manifest.get("paw")
    has_extension_app = isinstance(paw, Mapping) and "extensionApp" in paw
    if paw is not None and not isinstance(paw, Mapping):
        raise TeamPackageError(
            "Pi package paw field must be an object",
            code="manifest_invalid",
        )
    return manifest_name, manifest, has_extension_app


def _verified_extension_app(
    package_root: Path,
    manifest: Mapping[str, object],
    *,
    package_id: str,
    version: str,
) -> dict[str, object] | None:
    paw = manifest.get("paw")
    if not isinstance(paw, Mapping) or "extensionApp" not in paw:
        return None
    capabilities = paw.get("capabilities")
    if (
        not isinstance(capabilities, list)
        or any(not isinstance(item, str) for item in capabilities)
        or len(set(capabilities)) != len(capabilities)
    ):
        raise TeamPackageError(
            "Pi package Extension App capabilities are invalid",
            code="extension_app_invalid",
        )
    # This helper is a pure content/binding validator.  It does not access the
    # Runtime or install anything.  Keep the import lazy so ordinary bundled
    # rag-ime-plugin.json packages have no Agent Extension lifecycle coupling.
    validation_root = package_root
    temporary_root: Path | None = None
    # The immutable storage marker lives beside package files so ``load`` can
    # verify the inventory.  The existing pure App validator intentionally
    # accepts only a native Pi package tree, so hide that controller marker
    # from its package walk without changing the stored source.
    if (package_root / _META_NAME).is_file():
        try:
            temporary_root = Path(
                tempfile.mkdtemp(prefix=".team-extension-check-", dir=package_root.parent)
            )
            validation_root = temporary_root / "package"
            shutil.copytree(
                package_root,
                validation_root,
                ignore=shutil.ignore_patterns(_META_NAME),
            )
        except OSError as exc:
            if temporary_root is not None:
                shutil.rmtree(temporary_root, ignore_errors=True)
            raise TeamPackageError(
                "Pi package Extension App evidence cannot be checked",
                code="extension_app_invalid",
            ) from exc
    try:
        from ..agent_extensions import _verified_extension_app_source

        evidence = _verified_extension_app_source(
            str(validation_root),
            "",
            package_id,
            version,
            tuple(sorted(capabilities)),
        )
    except (OSError, TypeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise TeamPackageError(
            "Pi package Extension App evidence is invalid",
            code="extension_app_invalid",
        ) from exc
    finally:
        if temporary_root is not None:
            shutil.rmtree(temporary_root, ignore_errors=True)
    if evidence is None:
        raise TeamPackageError(
            "Pi package Extension App evidence is invalid",
            code="extension_app_invalid",
        )
    return dict(evidence)


def _public_metadata(
    entry: Mapping[str, object],
    version_record: Mapping[str, object] | None,
    *,
    package_id: str,
    version: str | None,
    manifest: Mapping[str, object] | None = None,
    manifest_name: str | None = None,
    extension_app: Mapping[str, object] | None = None,
) -> dict[str, object]:
    source = entry.get("source")
    source_mapping = source if isinstance(source, Mapping) else {}
    source_kind = source_mapping.get("kind")
    source_label = source_mapping.get("label")
    result: dict[str, object] = {
        "id": package_id,
        "packageId": package_id,
        "displayName": _required_text(
            entry.get("displayName") or (manifest or {}).get("displayName")
            or (manifest or {}).get("name")
            or package_id,
            label="package display name",
            maximum=512,
        ),
        "description": str(
            entry.get("description")
            or (manifest or {}).get("description")
            or ""
        )[:4096],
        "publisher": str(entry.get("publisher") or "")[:512],
        "source": {
            "kind": str(source_kind or "bundled"),
            "label": str(source_label or "Product bundle"),
        },
        "permissions": [
            str(value)
            for value in (entry.get("permissions") or (manifest or {}).get("permissions") or [])
            if isinstance(value, str)
        ],
        "compatibility": dict(entry.get("compatibility") or {})
        if isinstance(entry.get("compatibility"), Mapping)
        else {},
        "security": {
            key: value
            for key, value in (entry.get("security") or {}).items()
            if key in {"reviewed", "networkAccess", "enforcement", "notes"}
        }
        if isinstance(entry.get("security"), Mapping)
        else {},
        "version": version,
        "installable": source_kind == "bundled" and version is not None,
        "distribution": "team_staged_source",
    }
    if version_record is not None:
        for key in ("releasedAt", "notes"):
            if key in version_record:
                result[key] = str(version_record.get(key) or "")[:4096]
    if manifest is not None:
        result["manifestName"] = manifest_name or NATIVE_MANIFEST
        pi = manifest.get("pi")
        if isinstance(pi, Mapping):
            resources: dict[str, list[str]] = {}
            for resource_kind in ("extensions", "skills", "prompts", "themes"):
                values = pi.get(resource_kind)
                if isinstance(values, list):
                    resources[resource_kind] = [
                        _normalize_manifest_reference(
                            value,
                            label=f"Pi package {resource_kind} reference",
                        )
                        for value in values
                        if isinstance(value, str)
                    ]
            if resources:
                result["resources"] = resources
    if extension_app is not None:
        result["extensionApp"] = dict(extension_app)
    _validate_public_metadata(result)
    return result


def _validate_public_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TeamPackageError(
            "staged package metadata is invalid",
            code="storage_tampered",
        )

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if str(key) in _PUBLIC_PATH_KEYS:
                    raise TeamPackageError(
                        "staged package metadata contains a private path",
                        code="storage_tampered",
                    )
                visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)

    visit(value)
    try:
        encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise TeamPackageError(
            "staged package metadata is not JSON serializable",
            code="storage_tampered",
        ) from exc
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise TeamPackageError(
            "staged package metadata exceeds its size limit",
            code="storage_tampered",
        )
    return dict(value)


class TeamPackageCatalog:
    """Prepare exact catalog versions under a caller-owned Team data root."""

    def __init__(
        self,
        source_root: Path,
        catalog_path: Path,
        storage_root: Path,
    ) -> None:
        self.source_root = _absolute_path(source_root, label="source_root")
        self.catalog_path = _absolute_path(catalog_path, label="catalog_path")
        self.storage_root = _absolute_path(storage_root, label="storage_root")
        _assert_no_symlink_path(self.source_root, label="source_root")
        source_info = os.lstat(self.source_root)
        if not stat.S_ISDIR(source_info.st_mode):
            raise TeamPackageError(
                "source_root must be a directory",
                code="trusted_root_invalid",
            )
        _assert_no_symlink_path(self.catalog_path, label="catalog_path")
        catalog_info = os.lstat(self.catalog_path)
        if not stat.S_ISREG(catalog_info.st_mode):
            raise TeamPackageError(
                "catalog_path must be a regular file",
                code="trusted_root_invalid",
            )
        if _is_within(self.storage_root, self.source_root) or _is_within(
            self.source_root, self.storage_root
        ):
            raise TeamPackageError(
                "storage_root must be separate from source_root",
                code="trusted_root_overlap",
            )
        _ensure_directory(self.storage_root, label="storage_root")
        self._lock = RLock()

    def list_available(self) -> list[dict[str, object]]:
        """Return catalog metadata without source paths or private roots."""

        with self._lock:
            rows = self._catalog_rows()
            result: list[dict[str, object]] = []
            for row in rows:
                package_id = str(row.entry["id"])
                version_record = row.version_record
                version = (
                    str(version_record.get("version") or "")
                    if version_record is not None
                    else None
                )
                result.append(
                    _public_metadata(
                        row.entry,
                        version_record,
                        package_id=package_id,
                        version=version,
                    )
                )
            return result

    def stage_install(self, package_id: str, version: str) -> StagedTeamPackage:
        """Copy one exact catalog version atomically into immutable storage."""

        package_id = _safe_identifier(package_id, label="package_id")
        with self._lock:
            if version == "":
                for row in self._catalog_rows():
                    if (
                        str(row.entry.get("id") or "") == package_id
                        and row.version_record is None
                    ):
                        raise TeamPackageError(
                            "plugin catalog item is available for review only",
                            code="package_not_installable",
                        )
            version = _safe_version(version, label="version")
            self._assert_storage_root()
            entry, version_record = self._select_catalog_version(package_id, version)
            existing = self._staged_version(package_id, version)
            source_path = version_record.get("sourcePath")
            parts = _safe_relative(source_path, label="catalog source path")
            files = self._snapshot_source(parts)
            manifest_name, manifest, has_extension_app = _validate_manifest(
                files,
                package_id=package_id,
                version=version,
            )
            digest = _package_digest(files)
            if existing is not None:
                if existing.digest != digest:
                    raise TeamPackageError(
                        'package version was already staged with different content',
                        code='duplicate_version',
                    )
                # An interrupted publish may have staged bytes without a DB
                # receipt. Reusing verified identical bytes lets it recover
                # without overwriting either content or frozen metadata.
                return existing
            final_path = self.storage_root / digest
            if os.path.lexists(final_path):
                # A digest is immutable.  Never replace an existing directory,
                # even when a caller's source now hashes to that digest.
                raise TeamPackageError(
                    "content-addressed package digest already exists",
                    code="digest_conflict",
                )
            try:
                temporary = Path(
                    tempfile.mkdtemp(prefix=".team-package-", dir=self.storage_root)
                )
            except OSError as exc:
                raise TeamPackageError(
                    "package staging directory cannot be created",
                    code="storage_unavailable",
                ) from exc
            package_destination = temporary / "package"
            package_destination.mkdir(mode=0o700)
            extension_app: dict[str, object] | None = None
            try:
                for item in files:
                    target = package_destination.joinpath(*item.relative_path.split("/"))
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                    descriptor = os.open(target, flags, 0o700 if item.executable else 0o600)
                    try:
                        with os.fdopen(descriptor, "wb") as output:
                            output.write(item.data)
                            output.flush()
                            os.fsync(output.fileno())
                    except Exception:
                        # fdopen owns the descriptor after construction.  The
                        # outer cleanup removes the incomplete staging tree.
                        raise
                if has_extension_app:
                    extension_app = _verified_extension_app(
                        package_destination,
                        manifest,
                        package_id=package_id,
                        version=version,
                    )
                metadata = _public_metadata(
                    entry,
                    version_record,
                    package_id=package_id,
                    version=version,
                    manifest=manifest,
                    manifest_name=manifest_name,
                    extension_app=extension_app,
                )
                marker = {
                    "schemaVersion": _META_SCHEMA_VERSION,
                    "packageId": package_id,
                    "version": version,
                    "digest": digest,
                    "manifestName": manifest_name,
                    "publicMetadata": metadata,
                    "files": [
                        {
                            "path": item.relative_path,
                            "size": len(item.data),
                            "sha256": hashlib.sha256(item.data).hexdigest(),
                        }
                        for item in sorted(files, key=lambda value: value.relative_path)
                    ],
                }
                marker_data = json.dumps(
                    marker,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                if len(marker_data) > MAX_METADATA_BYTES:
                    raise TeamPackageError(
                        "staged package metadata exceeds its size limit",
                        code="metadata_size_limit",
                    )
                marker_path = temporary / _META_NAME
                marker_fd = os.open(
                    marker_path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    with os.fdopen(marker_fd, "wb") as marker_file:
                        marker_file.write(marker_data)
                        marker_file.flush()
                        os.fsync(marker_file.fileno())
                except Exception:
                    raise
                try:
                    os.rename(temporary, final_path)
                except FileExistsError as exc:
                    raise TeamPackageError(
                        "content-addressed package digest already exists",
                        code="digest_conflict",
                    ) from exc
                except OSError as exc:
                    if os.path.lexists(final_path):
                        raise TeamPackageError(
                            "content-addressed package digest already exists",
                            code="digest_conflict",
                        ) from exc
                    raise TeamPackageError(
                        "package commit failed before it became visible",
                        code="storage_unavailable",
                    ) from exc
                _fsync_directory(self.storage_root)
            except TeamPackageError:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
            except (OSError, ValueError, TypeError) as exc:
                shutil.rmtree(temporary, ignore_errors=True)
                raise TeamPackageError(
                    "package staging failed before commit",
                    code="stage_failed",
                ) from exc
            return StagedTeamPackage(
                package_id=package_id,
                version=version,
                digest=digest,
                path=final_path / "package",
                public_metadata=metadata,
                manifest=dict(manifest),
                manifest_name=manifest_name,
                extension_app=extension_app,
            )

    def load(self, digest: str) -> StagedTeamPackage:
        """Verify and load one previously committed immutable package."""

        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise TeamPackageError(
                "package digest must be a lowercase SHA-256",
                code="digest_invalid",
            )
        with self._lock:
            self._assert_storage_root()
            return self._load_unlocked(digest)

    def _assert_storage_root(self) -> None:
        _assert_no_symlink_path(self.storage_root, label="storage_root")
        try:
            info = os.lstat(self.storage_root)
        except OSError as exc:
            raise TeamPackageError(
                "package storage is unavailable",
                code="storage_unavailable",
            ) from exc
        if not stat.S_ISDIR(info.st_mode):
            raise TeamPackageError(
                "package storage must be a directory",
                code="storage_unavailable",
            )

    def _catalog_document(self) -> dict[str, object]:
        data = _read_regular(
            self.catalog_path,
            maximum=MAX_CATALOG_BYTES,
            label="plugin catalog",
        )
        document = _json_object(data, label="plugin catalog", maximum=MAX_CATALOG_BYTES)
        if document.get("schemaVersion") != CATALOG_SCHEMA:
            raise TeamPackageError(
                "plugin catalog schema is unsupported",
                code="catalog_invalid",
            )
        entries = document.get("entries")
        if not isinstance(entries, list) or len(entries) > MAX_CATALOG_ENTRIES:
            raise TeamPackageError(
                "plugin catalog entries are invalid or exceed the limit",
                code="catalog_invalid",
            )
        return document

    def _catalog_rows(self) -> list[_CatalogRow]:
        document = self._catalog_document()
        rows: list[_CatalogRow] = []
        seen_ids: set[str] = set()
        seen_versions: set[tuple[str, str]] = set()
        for raw_entry in document["entries"]:
            if not isinstance(raw_entry, Mapping):
                raise TeamPackageError(
                    "plugin catalog entry must be an object",
                    code="catalog_invalid",
                )
            package_id = _safe_identifier(raw_entry.get("id"), label="catalog package id")
            if package_id in seen_ids:
                raise TeamPackageError(
                    "plugin catalog contains duplicate package ids",
                    code="catalog_invalid",
                )
            seen_ids.add(package_id)
            entry = dict(raw_entry)
            source = entry.get("source")
            if not isinstance(source, Mapping):
                raise TeamPackageError(
                    "plugin catalog source must be an object",
                    code="catalog_invalid",
                )
            source_kind = source.get("kind")
            if not isinstance(source_kind, str) or not source_kind:
                raise TeamPackageError(
                    "plugin catalog source kind is invalid",
                    code="catalog_invalid",
                )
            versions = entry.get("versions", [])
            if not isinstance(versions, list) or len(versions) > MAX_CATALOG_ENTRIES:
                raise TeamPackageError(
                    "plugin catalog versions are invalid or exceed the limit",
                    code="catalog_invalid",
                )
            if not versions:
                if source_kind != "review_only":
                    raise TeamPackageError(
                        "catalog package without a version must be review only",
                        code="catalog_invalid",
                    )
                rows.append(_CatalogRow(entry, None))
                continue
            for raw_version in versions:
                if not isinstance(raw_version, Mapping):
                    raise TeamPackageError(
                        "plugin catalog version must be an object",
                        code="catalog_invalid",
                    )
                version = _safe_version(
                    raw_version.get("version"), label="catalog package version"
                )
                key = (package_id, version)
                if key in seen_versions:
                    raise TeamPackageError(
                        "plugin catalog contains duplicate package versions",
                        code="catalog_invalid",
                    )
                seen_versions.add(key)
                version_record = dict(raw_version)
                if source_kind == "bundled":
                    _safe_relative(
                        version_record.get("sourcePath"),
                        label="catalog source path",
                    )
                rows.append(_CatalogRow(entry, version_record))
        return rows

    def _select_catalog_version(
        self, package_id: str, version: str
    ) -> tuple[dict[str, object], dict[str, object]]:
        package_seen = False
        for row in self._catalog_rows():
            if str(row.entry.get("id") or "") != package_id:
                continue
            package_seen = True
            if row.version_record is None:
                raise TeamPackageError(
                    "plugin catalog item is available for review only",
                    code="package_not_installable",
                )
            if str(row.version_record.get("version") or "") != version:
                continue
            source = row.entry.get("source")
            if not isinstance(source, Mapping) or source.get("kind") != "bundled":
                raise TeamPackageError(
                    "plugin catalog item is available for review only",
                    code="package_not_installable",
                )
            return row.entry, row.version_record
        if package_seen:
            raise TeamPackageError(
                "plugin catalog version does not exist",
                code="version_not_found",
            )
        raise TeamPackageError(
            "plugin catalog package does not exist",
            code="package_not_found",
        )

    def _snapshot_source(self, parts: Sequence[str]) -> list[_SnapshotFile]:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(self.source_root, flags)
        except OSError as exc:
            raise TeamPackageError(
                "catalog source root cannot be opened safely",
                code="source_unavailable",
            ) from exc
        try:
            for part in parts:
                try:
                    child_fd = os.open(part, flags, dir_fd=directory_fd)
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        raise TeamPackageError(
                            "catalog source path contains a symbolic link",
                            code="symbolic_link",
                        ) from exc
                    raise TeamPackageError(
                        "catalog source path is missing or not a directory",
                        code="source_path_invalid",
                    ) from exc
                os.close(directory_fd)
                directory_fd = child_fd
            info = os.fstat(directory_fd)
            if not stat.S_ISDIR(info.st_mode):
                raise TeamPackageError(
                    "catalog source path must be a regular directory",
                    code="source_path_invalid",
                )
            files: list[_SnapshotFile] = []
            self._walk_directory_fd(directory_fd, (), files, include_ignored=True)
            if not files:
                raise TeamPackageError(
                    "catalog source package is empty",
                    code="source_empty",
                )
            return files
        except TeamPackageError:
            raise
        except OSError as exc:
            raise TeamPackageError(
                "catalog source cannot be read safely",
                code="source_unavailable",
            ) from exc
        finally:
            os.close(directory_fd)

    def _walk_directory_fd(
        self,
        directory_fd: int,
        prefix: tuple[str, ...],
        files: list[_SnapshotFile],
        *,
        include_ignored: bool,
    ) -> None:
        try:
            entries = sorted(
                list(os.scandir(directory_fd)),
                key=lambda item: (item.name.casefold(), item.name),
            )
        except OSError as exc:
            raise TeamPackageError(
                "package directory cannot be read safely",
                code="source_unavailable",
            ) from exc
        names: set[str] = set()
        for entry in entries:
            name = _safe_segment(entry.name, label="package path")
            folded = name.casefold()
            if folded in names:
                raise TeamPackageError(
                    "package contains case-colliding paths",
                    code="path_invalid",
                )
            names.add(folded)
            relative_parts = prefix + (name,)
            relative = "/".join(relative_parts)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise TeamPackageError(
                    "package entry cannot be inspected safely",
                    code="source_unavailable",
                ) from exc
            mode = info.st_mode
            if stat.S_ISLNK(mode):
                raise TeamPackageError(
                    f"package contains a symbolic link: {relative}",
                    code="symbolic_link",
                )
            if stat.S_ISDIR(mode):
                if name in _IGNORED_DIRECTORIES and include_ignored:
                    continue
                flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
                try:
                    child_fd = os.open(name, flags, dir_fd=directory_fd)
                except OSError as exc:
                    raise TeamPackageError(
                        f"package directory cannot be opened: {relative}",
                        code="source_unavailable",
                    ) from exc
                try:
                    self._walk_directory_fd(
                        child_fd,
                        relative_parts,
                        files,
                        include_ignored=include_ignored,
                    )
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(mode):
                raise TeamPackageError(
                    f"package entry must be a regular file: {relative}",
                    code="special_file",
                )
            if name in _IGNORED_DIRECTORIES:
                continue
            if name == _META_NAME:
                raise TeamPackageError(
                    "package uses a reserved metadata filename",
                    code="path_invalid",
                )
            if _is_sensitive_name(name):
                raise TeamPackageError(
                    f"package contains a sensitive environment or credential file: {relative}",
                    code="sensitive_file",
                )
            data, executable = _read_fd_file(
                directory_fd,
                name,
                maximum=MAX_PACKAGE_FILE_BYTES,
                label=f"package file {relative}",
            )
            if len(files) >= MAX_PACKAGE_FILES:
                raise TeamPackageError(
                    "package exceeds the file count limit",
                    code="file_count_limit",
                )
            if sum(len(item.data) for item in files) + len(data) > MAX_PACKAGE_BYTES:
                raise TeamPackageError(
                    "package exceeds the total size limit",
                    code="package_size_limit",
                )
            files.append(_SnapshotFile(relative, data, executable))

    def _staged_version(self, package_id: str, version: str) -> StagedTeamPackage | None:
        try:
            entries = list(os.scandir(self.storage_root))
        except OSError as exc:
            raise TeamPackageError(
                "package storage cannot be read safely",
                code="storage_unavailable",
            ) from exc
        for entry in entries:
            if entry.name.startswith(".") or not _SHA256.fullmatch(entry.name):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise TeamPackageError(
                    "package storage entry cannot be inspected",
                    code="storage_tampered",
                ) from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise TeamPackageError(
                    "package storage contains an invalid content directory",
                    code="storage_tampered",
                )
            # Validate every existing content directory before deciding that a
            # package/version is free.  A damaged directory must never be
            # silently bypassed by staging a second copy of that version.
            existing = self._load_unlocked(entry.name)
            if existing.package_id == package_id and existing.version == version:
                return existing
        return None

    def _load_unlocked(self, digest: str) -> StagedTeamPackage:
        final_path = self.storage_root / digest
        try:
            info = os.lstat(final_path)
        except FileNotFoundError as exc:
            raise TeamPackageError(
                "staged package digest does not exist",
                code="package_not_found",
            ) from exc
        except OSError as exc:
            raise TeamPackageError(
                "staged package cannot be inspected",
                code="storage_unavailable",
            ) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise TeamPackageError(
                "staged package directory is not safe",
                code="storage_tampered",
            )
        marker_path = final_path / _META_NAME
        marker = _json_object(
            _read_regular(marker_path, maximum=MAX_METADATA_BYTES, label="package metadata"),
            label="package metadata",
            maximum=MAX_METADATA_BYTES,
        )
        if marker.get("schemaVersion") != _META_SCHEMA_VERSION:
            raise TeamPackageError(
                "staged package metadata schema is unsupported",
                code="storage_tampered",
            )
        package_path = final_path / "package"
        try:
            package_info = os.lstat(package_path)
        except OSError as exc:
            raise TeamPackageError(
                "staged package content directory is missing",
                code="integrity_failure",
            ) from exc
        if stat.S_ISLNK(package_info.st_mode) or not stat.S_ISDIR(package_info.st_mode):
            raise TeamPackageError(
                "staged package content directory is not safe",
                code="integrity_failure",
            )
        try:
            top_level = list(os.scandir(final_path))
            names = {item.name for item in top_level}
            if names != {_META_NAME, "package"} or any(
                item.is_symlink() for item in top_level
            ):
                raise TeamPackageError(
                    "staged package storage layout is invalid",
                    code="integrity_failure",
                )
        except TeamPackageError:
            raise
        except OSError as exc:
            raise TeamPackageError(
                "staged package storage layout cannot be inspected",
                code="storage_tampered",
            ) from exc
        if marker.get("digest") != digest:
            raise TeamPackageError(
                "staged package metadata digest does not match its directory",
                code="integrity_failure",
            )
        package_id = _safe_identifier(marker.get("packageId"), label="staged package id")
        version = _safe_version(marker.get("version"), label="staged package version")
        manifest_name = marker.get("manifestName")
        if manifest_name not in {PACKAGE_MANIFEST, NATIVE_MANIFEST}:
            raise TeamPackageError(
                "staged package manifest name is invalid",
                code="storage_tampered",
            )
        raw_files = marker.get("files")
        if not isinstance(raw_files, list) or not raw_files or len(raw_files) > MAX_PACKAGE_FILES:
            raise TeamPackageError(
                "staged package file inventory is invalid",
                code="storage_tampered",
            )
        expected: dict[str, tuple[int, str]] = {}
        for raw_file in raw_files:
            if not isinstance(raw_file, Mapping):
                raise TeamPackageError(
                    "staged package file inventory is invalid",
                    code="storage_tampered",
                )
            parts = _safe_relative(raw_file.get("path"), label="staged package file path")
            relative = "/".join(parts)
            if relative == _META_NAME or relative in expected:
                raise TeamPackageError(
                    "staged package file inventory contains duplicates",
                    code="storage_tampered",
                )
            size = raw_file.get("size")
            checksum = raw_file.get("sha256")
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
                or size > MAX_PACKAGE_FILE_BYTES
                or not isinstance(checksum, str)
                or not _SHA256.fullmatch(checksum)
            ):
                raise TeamPackageError(
                    "staged package file inventory is invalid",
                    code="storage_tampered",
                )
            expected[relative] = (size, checksum)

        files: list[_SnapshotFile] = []
        self._walk_directory_fd_by_path(package_path, (), files, skip_marker=False)
        actual = {item.relative_path: item for item in files}
        if set(actual) != set(expected):
            raise TeamPackageError(
                "staged package file inventory does not match stored files",
                code="integrity_failure",
            )
        for item in files:
            size, checksum = expected[item.relative_path]
            if len(item.data) != size or hashlib.sha256(item.data).hexdigest() != checksum:
                raise TeamPackageError(
                    "staged package file integrity check failed",
                    code="integrity_failure",
                )
        if _package_digest(files) != digest:
            raise TeamPackageError(
                "staged package digest integrity check failed",
                code="integrity_failure",
            )
        manifest_file = next(
            (item for item in files if item.relative_path == manifest_name),
            None,
        )
        if manifest_file is None:
            raise TeamPackageError(
                "staged package manifest is missing",
                code="integrity_failure",
            )
        checked_manifest_name, manifest, has_extension_app = _validate_manifest(
            files,
            package_id=package_id,
            version=version,
        )
        if checked_manifest_name != manifest_name:
            raise TeamPackageError(
                "staged package manifest name changed",
                code="integrity_failure",
            )
        extension_app: dict[str, object] | None = None
        if has_extension_app:
            extension_app = _verified_extension_app(
                package_path,
                manifest,
                package_id=package_id,
                version=version,
            )
        metadata = _validate_public_metadata(marker.get("publicMetadata"))
        if metadata.get("packageId") != package_id or metadata.get("version") != version:
            raise TeamPackageError(
                "staged package metadata does not match its manifest",
                code="integrity_failure",
            )
        metadata_extension = metadata.get("extensionApp")
        if extension_app is not None and metadata_extension != extension_app:
            raise TeamPackageError(
                "staged Extension App evidence changed",
                code="integrity_failure",
            )
        if extension_app is None and metadata_extension is not None:
            raise TeamPackageError(
                "staged package contains unexpected Extension App evidence",
                code="integrity_failure",
            )
        return StagedTeamPackage(
            package_id=package_id,
            version=version,
            digest=digest,
            path=package_path,
            public_metadata=metadata,
            manifest=dict(manifest),
            manifest_name=manifest_name,
            extension_app=extension_app,
        )

    def _walk_directory_fd_by_path(
        self,
        root: Path,
        prefix: tuple[str, ...],
        files: list[_SnapshotFile],
        *,
        skip_marker: bool,
    ) -> None:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(root, flags)
        except OSError as exc:
            raise TeamPackageError(
                "staged package directory cannot be opened safely",
                code="storage_tampered",
            ) from exc
        try:
            entries = sorted(
                list(os.scandir(directory_fd)),
                key=lambda item: (item.name.casefold(), item.name),
            )
            names: set[str] = set()
            for entry in entries:
                name = _safe_segment(entry.name, label="staged package path")
                folded = name.casefold()
                if folded in names:
                    raise TeamPackageError(
                        "staged package contains case-colliding paths",
                        code="integrity_failure",
                    )
                names.add(folded)
                relative_parts = prefix + (name,)
                relative = "/".join(relative_parts)
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise TeamPackageError(
                        "staged package contains a symbolic link",
                        code="integrity_failure",
                    )
                if stat.S_ISDIR(info.st_mode):
                    self._walk_directory_fd_by_path(
                        root / name,
                        relative_parts,
                        files,
                        skip_marker=skip_marker,
                    )
                elif stat.S_ISREG(info.st_mode):
                    if skip_marker and not prefix and name == _META_NAME:
                        continue
                    data, executable = _read_fd_file(
                        directory_fd,
                        name,
                        maximum=MAX_PACKAGE_FILE_BYTES,
                        label=f"staged package file {relative}",
                    )
                    if len(files) >= MAX_PACKAGE_FILES:
                        raise TeamPackageError(
                            "staged package exceeds the file count limit",
                            code="integrity_failure",
                        )
                    if sum(len(item.data) for item in files) + len(data) > MAX_PACKAGE_BYTES:
                        raise TeamPackageError(
                            "staged package exceeds the total size limit",
                            code="integrity_failure",
                        )
                    files.append(_SnapshotFile(relative, data, executable))
                else:
                    raise TeamPackageError(
                        "staged package contains a special file",
                        code="integrity_failure",
                    )
        except TeamPackageError:
            raise
        except OSError as exc:
            raise TeamPackageError(
                "staged package directory cannot be read safely",
                code="storage_tampered",
            ) from exc
        finally:
            os.close(directory_fd)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        os.close(descriptor)


__all__ = [
    "CATALOG_SCHEMA",
    "MAX_PACKAGE_FILES",
    "MAX_PACKAGE_FILE_BYTES",
    "MAX_PACKAGE_BYTES",
    "StagedTeamPackage",
    "TeamPackageCatalog",
    "TeamPackageError",
]
