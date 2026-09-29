from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator, Mapping

from .agent_tool_ids import CONTROL_TOOL_IDS
from .contracts.json_schema import ContractValidationError, validate_contract


MANIFEST_SCHEMA_VERSION = "rag-ime.pi-runtime-manifest.v1"
POINTER_SCHEMA_VERSION = "rag-ime.pi-runtime-pointer.v1"
RETENTION_SCHEMA_VERSION = "rag-ime.pi-runtime-retention.v1"
LIFECYCLE_SCHEMA_VERSION = "rag-ime.pi-runtime-lifecycle.v1"
ACCEPTANCE_SCHEMA_VERSION = "rag-ime.pi-session-staged-runtime-e2e.v1"
MANIFEST_NAME = "manifest.json"
POINTER_NAME = "current.json"
LOCK_NAME = ".managed-pi-runtime.lock"
QUARANTINE_NAME = ".retention-quarantine"
QUARANTINE_RECEIPT_NAME = "retention-batch.json"
QUARANTINE_RECEIPT_SCHEMA_VERSION = "rag-ime.pi-runtime-retention-batch.v1"
_MANIFEST_CONTRACT = "pi-runtime-manifest.v1.json"
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_RETENTION_BATCH_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_TOOL_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RUNTIME_METHOD_PATTERN = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_RUNTIME_FILES = 100_000
_MAX_RUNTIME_METHODS = 64
_MAX_RETIRED_GENERATIONS = 64
REQUIRED_ACCEPTANCE_METHODS = (
    "session.open",
    "session.prompt",
    "session.steer",
    "session.debug.context",
    "session.abort",
    "session.snapshot",
)
REQUIRED_SESSION_RUNTIME_METHODS = REQUIRED_ACCEPTANCE_METHODS


class ManagedPiRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ManagedPiRuntimeInstallation:
    runtime_root: Path
    runtime_dir: Path
    runtime_version: str
    pi_version: str
    executable: Path
    node_executable: str
    extension_path: Path
    tools: tuple[str, ...]
    manifest_sha256: str
    protocol_version: str = "2"
    runtime_methods: tuple[str, ...] = ()


def discover_managed_pi_runtime(
    app_support: str | Path,
    *,
    expected_pi_version: str = "",
) -> ManagedPiRuntimeInstallation:
    with _managed_runtime_lock(
        app_support,
        create_runtime_root=False,
    ) as runtime_root:
        return _discover_managed_pi_runtime_locked(
            runtime_root,
            expected_pi_version=expected_pi_version,
        )


def snapshot_managed_pi_runtime(
    app_support: str | Path,
    *,
    expected_pi_version: str = "",
    maximum_attempts: int = 3,
) -> ManagedPiRuntimeInstallation:
    """Verify the active immutable runtime without opening its lifecycle lock.

    Production discovery must keep using :func:`discover_managed_pi_runtime` so
    activation, rollback, and retention share one lifecycle lock.  Read-only
    evaluation sandboxes may lack permission to create or chmod that lock.  For
    those callers, verify every manifest-bound file and accept the snapshot only
    when the atomic pointer has identical bytes before and after verification.
    """

    if maximum_attempts < 1:
        raise ManagedPiRuntimeError(
            "managed Pi read-only snapshot maximum attempts must be positive"
        )
    app_root_input = Path(app_support).expanduser()
    if app_root_input.is_symlink():
        raise ManagedPiRuntimeError(
            "managed Pi application support root must not be a symlink"
        )
    try:
        app_root = app_root_input.resolve(strict=True)
    except OSError as exc:
        raise ManagedPiRuntimeError(
            "managed Pi application support root is missing"
        ) from exc
    if not app_root.is_dir():
        raise ManagedPiRuntimeError(
            "managed Pi application support root must be a real directory"
        )
    runtime_input = app_root / "PiRuntime"
    if runtime_input.is_symlink():
        raise ManagedPiRuntimeError("managed Pi runtime root must not be a symlink")
    try:
        runtime_root = runtime_input.resolve(strict=True)
    except OSError as exc:
        raise ManagedPiRuntimeError("managed Pi runtime root is missing") from exc
    if not runtime_root.is_dir():
        raise ManagedPiRuntimeError("managed Pi runtime root must be a real directory")

    pointer_path = runtime_root / POINTER_NAME
    for _attempt in range(maximum_attempts):
        if not pointer_path.is_file() or pointer_path.is_symlink():
            raise ManagedPiRuntimeError("managed Pi runtime pointer is missing")
        pointer_before = _read_limited_bytes(pointer_path)
        pointer = _parse_json_object(pointer_before, pointer_path)
        if pointer.get("schemaVersion") != POINTER_SCHEMA_VERSION:
            raise ManagedPiRuntimeError(
                "managed Pi runtime pointer schema is unsupported"
            )
        version = _safe_version(
            pointer.get("version"),
            label="runtime pointer version",
        )
        manifest_sha256 = _sha256_text(
            pointer.get("manifestSha256"),
            label="runtime manifest digest",
        )
        try:
            installation = _load_installation(
                runtime_root=runtime_root,
                runtime_dir=runtime_root / version,
                expected_manifest_sha256=manifest_sha256,
                expected_pi_version=expected_pi_version,
                verify_all_files=True,
            )
        except ManagedPiRuntimeError:
            try:
                pointer_after_failure = _read_limited_bytes(pointer_path)
            except ManagedPiRuntimeError:
                continue
            if pointer_after_failure != pointer_before:
                continue
            raise
        try:
            pointer_after = _read_limited_bytes(pointer_path)
        except ManagedPiRuntimeError:
            continue
        if pointer_after == pointer_before:
            _require_supported_protocol(installation.protocol_version)
            return installation
    raise ManagedPiRuntimeError(
        "managed Pi runtime pointer changed during read-only snapshot"
    )


def snapshot_managed_pi_runtime_payload(
    payload: str | Path,
    *,
    expected_pi_version: str = "",
) -> ManagedPiRuntimeInstallation:
    """Verify one explicit immutable payload without installing or reading Current.json."""

    payload_input = Path(payload).expanduser()
    if payload_input.is_symlink():
        raise ManagedPiRuntimeError("managed Pi runtime payload must not be a symlink")
    try:
        payload_root = payload_input.resolve(strict=True)
    except OSError as exc:
        raise ManagedPiRuntimeError("managed Pi runtime payload is missing") from exc
    if not payload_root.is_dir():
        raise ManagedPiRuntimeError("managed Pi runtime payload must be a real directory")
    manifest_path = payload_root / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ManagedPiRuntimeError("managed Pi runtime manifest is missing")
    manifest_bytes = _read_limited_bytes(manifest_path)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    manifest = _parse_json_object(manifest_bytes, manifest_path)
    runtime_version = _safe_version(
        manifest.get("runtimeVersion"),
        label="runtime version",
    )
    installation = _load_installation(
        runtime_root=payload_root.parent,
        runtime_dir=payload_root,
        expected_manifest_sha256=manifest_sha256,
        expected_pi_version=expected_pi_version,
        verify_all_files=True,
        expected_runtime_version=runtime_version,
    )
    _require_supported_protocol(installation.protocol_version)
    return installation


def inspect_managed_pi_runtime(
    app_support: str | Path,
    *,
    verify_all_files: bool = True,
) -> tuple[
    ManagedPiRuntimeInstallation,
    dict[str, object],
    dict[str, object],
]:
    """Return one lock-consistent installation, pointer, and manifest snapshot."""

    with _managed_runtime_lock(
        app_support,
        create_runtime_root=False,
    ) as runtime_root:
        pointer_path = runtime_root / POINTER_NAME
        if not pointer_path.is_file() or pointer_path.is_symlink():
            raise ManagedPiRuntimeError("managed Pi runtime pointer is missing")
        pointer = _read_json_object(pointer_path)
        if pointer.get("schemaVersion") != POINTER_SCHEMA_VERSION:
            raise ManagedPiRuntimeError(
                "managed Pi runtime pointer schema is unsupported"
            )
        version = _safe_version(
            pointer.get("version"),
            label="runtime pointer version",
        )
        manifest_sha256 = _sha256_text(
            pointer.get("manifestSha256"),
            label="runtime manifest digest",
        )
        installation = _load_installation(
            runtime_root=runtime_root,
            runtime_dir=runtime_root / version,
            expected_manifest_sha256=manifest_sha256,
            expected_pi_version="",
            verify_all_files=verify_all_files,
        )
        manifest = _read_json_object(installation.runtime_dir / MANIFEST_NAME)
        return installation, pointer, manifest


def _discover_managed_pi_runtime_locked(
    runtime_root: Path,
    *,
    expected_pi_version: str = "",
) -> ManagedPiRuntimeInstallation:
    pointer_path = runtime_root / POINTER_NAME
    if not pointer_path.is_file() or pointer_path.is_symlink():
        raise ManagedPiRuntimeError("managed Pi runtime pointer is missing")
    pointer = _read_json_object(pointer_path)
    if pointer.get("schemaVersion") != POINTER_SCHEMA_VERSION:
        raise ManagedPiRuntimeError("managed Pi runtime pointer schema is unsupported")
    version = _safe_version(pointer.get("version"), label="runtime pointer version")
    manifest_sha256 = _sha256_text(pointer.get("manifestSha256"), label="runtime manifest digest")
    runtime_dir = runtime_root / version
    installation = _load_installation(
        runtime_root=runtime_root,
        runtime_dir=runtime_dir,
        expected_manifest_sha256=manifest_sha256,
        expected_pi_version=expected_pi_version,
        verify_all_files=True,
    )
    _require_supported_protocol(installation.protocol_version)
    return installation


def install_managed_pi_runtime(
    payload_dir: str | Path,
    app_support: str | Path,
    *,
    activate: bool = True,
    acceptance: Mapping[str, object] | None = None,
) -> ManagedPiRuntimeInstallation:
    source = Path(payload_dir).expanduser().resolve(strict=True)
    if not source.is_dir() or source.is_symlink():
        raise ManagedPiRuntimeError("managed Pi payload must be a real directory")
    source_manifest = source / MANIFEST_NAME
    if not source_manifest.is_file() or source_manifest.is_symlink():
        raise ManagedPiRuntimeError("managed Pi payload manifest is missing")
    manifest_bytes = _read_limited_bytes(source_manifest)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    manifest = _parse_json_object(manifest_bytes, source_manifest)
    source_provenance = manifest.get("source")
    if (
        isinstance(source_provenance, Mapping)
        and source_provenance.get("evaluationOnly") is True
    ):
        raise ManagedPiRuntimeError(
            "evaluation-only managed Pi runtime cannot be installed"
        )
    version = _safe_version(manifest.get("runtimeVersion"), label="runtime version")

    # Verify the complete payload before the application support tree changes.
    verified = _load_installation(
        runtime_root=source.parent,
        runtime_dir=source,
        expected_manifest_sha256=manifest_sha256,
        expected_pi_version="",
        verify_all_files=True,
        expected_runtime_version=version,
    )
    _require_supported_protocol(verified.protocol_version)

    with _managed_runtime_lock(app_support) as runtime_root:
        destination = runtime_root / version
        staging = runtime_root / f".{version}.staging-{uuid.uuid4().hex}"
        try:
            if destination.exists():
                installed_manifest = destination / MANIFEST_NAME
                if (
                    not installed_manifest.is_file()
                    or installed_manifest.is_symlink()
                    or _sha256_file(installed_manifest) != manifest_sha256
                ):
                    raise ManagedPiRuntimeError(
                        "managed Pi runtime version already exists with different contents"
                    )
            else:
                staging.mkdir(mode=0o700)
                for item in _manifest_file_items(manifest):
                    relative = _safe_relative_path(item.get("path"))
                    source_file = _resolve_payload_file(source, relative)
                    target_file = staging.joinpath(*relative.parts)
                    target_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copyfile(source_file, target_file)
                    os.chmod(
                        target_file,
                        0o755 if bool(item.get("executable")) else 0o644,
                    )
                _atomic_write_bytes(
                    staging / MANIFEST_NAME,
                    manifest_bytes,
                    mode=0o644,
                )
                _load_installation(
                    runtime_root=runtime_root,
                    runtime_dir=staging,
                    expected_manifest_sha256=manifest_sha256,
                    expected_pi_version="",
                    verify_all_files=True,
                    expected_runtime_version=version,
                )
                os.replace(staging, destination)
                _fsync_directory(runtime_root)
            installation = _load_installation(
                runtime_root=runtime_root,
                runtime_dir=destination,
                expected_manifest_sha256=manifest_sha256,
                expected_pi_version="",
                verify_all_files=True,
            )
            if activate:
                _activate_managed_pi_runtime_locked(
                    runtime_root,
                    installation,
                    acceptance=acceptance,
                )
                return _discover_managed_pi_runtime_locked(runtime_root)
            return installation
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def build_managed_pi_runtime_retention_plan(
    app_support: str | Path,
    *,
    retain_generations: int = 2,
) -> dict[str, object]:
    """Plan removal only for generations in the accepted retired lineage."""

    with _managed_runtime_lock(
        app_support,
        create_runtime_root=False,
    ) as runtime_root:
        return _build_managed_pi_runtime_retention_plan_locked(
            app_support=app_support,
            runtime_root=runtime_root,
            retain_generations=retain_generations,
        )


def _build_managed_pi_runtime_retention_plan_locked(
    *,
    app_support: str | Path,
    runtime_root: Path,
    retain_generations: int,
) -> dict[str, object]:
    """Build a plan while the caller holds the one lifecycle lock."""

    if retain_generations != 2:
        raise ManagedPiRuntimeError(
            "managed Pi retention currently requires exactly two generations"
        )
    active = _discover_managed_pi_runtime_locked(runtime_root)
    pointer_path = runtime_root / POINTER_NAME
    pointer = _read_json_object(pointer_path)
    previous, previous_reason = _pointer_previous_installation(runtime_root, pointer)
    lifecycle, lifecycle_reason = _pointer_lifecycle(pointer)
    retired_by_version = {
        str(item["version"]): item
        for item in (
            lifecycle.get("retiredGenerations", [])
            if lifecycle is not None
            else []
        )
        if isinstance(item, Mapping)
    }
    retention_blocked_reason = lifecycle_reason
    if retired_by_version and previous is None:
        retention_blocked_reason = previous_reason

    protected_versions = {active.runtime_version}
    if previous is not None:
        protected_versions.add(previous.runtime_version)

    actions = _pending_quarantine_cleanup_actions(
        runtime_root,
        protected_versions=protected_versions,
        retired_versions=set(retired_by_version),
    )
    for entry in sorted(runtime_root.iterdir(), key=lambda item: item.name):
        if entry.name in {POINTER_NAME, QUARANTINE_NAME}:
            continue
        target = str(entry)
        if entry.is_symlink():
            actions.append(
                {
                    "kind": "preserve_entry",
                    "target": target,
                    "status": "preserved_symlink",
                    "reason": "retention never follows or removes symlinks",
                }
            )
            continue
        if not entry.is_dir():
            actions.append(
                {
                    "kind": "preserve_entry",
                    "target": target,
                    "status": "preserved_unowned",
                    "reason": "entry is not a managed Pi generation directory",
                }
            )
            continue
        try:
            version = _safe_version(entry.name, label="runtime directory version")
        except ManagedPiRuntimeError as exc:
            actions.append(
                {
                    "kind": "preserve_entry",
                    "target": target,
                    "status": "preserved_unowned",
                    "reason": str(exc),
                }
            )
            continue
        if version in protected_versions:
            protected = active if version == active.runtime_version else previous
            actions.append(
                {
                    "kind": "preserve_generation",
                    "target": target,
                    "version": version,
                    "manifestSha256": (
                        protected.manifest_sha256 if protected is not None else ""
                    ),
                    "status": (
                        "retained_active"
                        if version == active.runtime_version
                        else "retained_previous"
                    ),
                    "reason": "active and immediate previous generations are rollback-safe",
                }
            )
            continue
        retired = retired_by_version.get(version)
        if retired is None:
            try:
                candidate = _verified_generation(runtime_root, entry)
            except (OSError, ManagedPiRuntimeError) as exc:
                actions.append(
                    {
                        "kind": "preserve_entry",
                        "target": target,
                        "status": "preserved_unverified",
                        "reason": str(exc),
                    }
                )
                continue
            actions.append(
                {
                    "kind": "preserve_generation",
                    "target": target,
                    "version": candidate.runtime_version,
                    "manifestSha256": candidate.manifest_sha256,
                    "status": "preserved_staged",
                    "reason": (
                        "generation is not in the accepted retired lineage; "
                        "it may be a --no-activate pre-stage or legacy generation"
                    ),
                }
            )
            continue
        try:
            candidate = _verified_generation(runtime_root, entry)
            retired_digest = _sha256_text(
                retired.get("manifestSha256"),
                label="retired runtime manifest digest",
            )
            if candidate.manifest_sha256 != retired_digest:
                raise ManagedPiRuntimeError(
                    "retired lineage digest does not match generation contents"
                )
        except (OSError, ManagedPiRuntimeError) as exc:
            actions.append(
                {
                    "kind": "preserve_generation",
                    "target": target,
                    "version": version,
                    "manifestSha256": str(retired.get("manifestSha256") or ""),
                    "status": "preserved_retired_unverified",
                    "reason": str(exc),
                }
            )
            continue
        if retention_blocked_reason:
            actions.append(
                {
                    "kind": "preserve_generation",
                    "target": target,
                    "version": candidate.runtime_version,
                    "manifestSha256": candidate.manifest_sha256,
                    "status": "preserved_retention_blocked",
                    "reason": retention_blocked_reason,
                }
            )
            continue
        directory_stat = entry.stat(follow_symlinks=False)
        actions.append(
            {
                "kind": "remove_generation",
                "target": target,
                "version": candidate.runtime_version,
                "manifestSha256": candidate.manifest_sha256,
                "directoryDevice": int(directory_stat.st_dev),
                "directoryInode": int(directory_stat.st_ino),
                "status": "planned",
                "reason": (
                    "generation is digest-bound in the explicit accepted retired lineage"
                ),
            }
        )

    planned_count = sum(
        action.get("kind") == "remove_generation"
        and action.get("status") == "planned"
        for action in actions
    )
    planned_cleanup_count = sum(
        action.get("kind") == "cleanup_quarantine_generation"
        and action.get("status") == "planned_cleanup"
        for action in actions
    )
    return {
        "schemaVersion": RETENTION_SCHEMA_VERSION,
        "mode": "dry-run",
        "ok": True,
        "appSupport": str(Path(app_support).expanduser().resolve(strict=False)),
        "runtimeRoot": str(runtime_root),
        "retainGenerations": retain_generations,
        "activeGeneration": _generation_receipt(active),
        "previousGeneration": (
            _generation_receipt(previous) if previous is not None else None
        ),
        "lifecycleSha256": (
            _canonical_json_sha256(lifecycle) if lifecycle is not None else ""
        ),
        "retiredLineageCount": len(retired_by_version),
        "retentionBlockedReason": retention_blocked_reason,
        "plannedRemovalCount": planned_count,
        "plannedCleanupCount": planned_cleanup_count,
        "actions": actions,
        "notes": [
            "Dry-run performs no filesystem mutation.",
            (
                "Only complete manifest-verified generations explicitly named in the "
                "digest-bound accepted retired lineage may be planned."
            ),
            (
                "The active pointer, active generation, immediate previous generation, "
                "--no-activate pre-stages, legacy generations, symlinks, and unverified "
                "entries are preserved."
            ),
            (
                "A digest- and inode-bound quarantine batch receipt makes interrupted "
                "post-pointer cleanup visible and retryable on the next dry-run."
            ),
        ],
    }


def apply_managed_pi_runtime_retention_plan(
    plan: Mapping[str, object],
    *,
    app_support: str | Path,
) -> dict[str, object]:
    """Apply an unchanged plan under the lifecycle lock with typed item receipts."""

    with _managed_runtime_lock(
        app_support,
        create_runtime_root=False,
    ) as runtime_root:
        expected_signature = _retention_plan_signature(
            plan,
            app_support=app_support,
        )
        fresh_plan = _build_managed_pi_runtime_retention_plan_locked(
            app_support=app_support,
            runtime_root=runtime_root,
            retain_generations=int(plan.get("retainGenerations") or 0),
        )
        fresh_signature = _retention_plan_signature(
            fresh_plan,
            app_support=app_support,
        )
        if fresh_signature != expected_signature:
            raise ManagedPiRuntimeError(
                "managed Pi retention state changed after dry-run; refusing removal"
            )

        cleanup_candidates = [
            action
            for action in fresh_plan["actions"]
            if isinstance(action, Mapping)
            and action.get("status") == "planned_cleanup"
        ]
        candidates = [
            action
            for action in fresh_plan["actions"]
            if isinstance(action, Mapping) and action.get("status") == "planned"
        ]
        cleaned_quarantine: set[tuple[str, str]] = set()
        pending_cleanup_failures: dict[tuple[str, str], str] = {}
        for action in cleanup_candidates:
            key = (
                str(action.get("batchId") or ""),
                str(action.get("version") or ""),
            )
            try:
                _apply_pending_quarantine_cleanup(runtime_root, action)
                cleaned_quarantine.add(key)
            except (OSError, ManagedPiRuntimeError) as exc:
                pending_cleanup_failures[key] = str(exc)
        if cleanup_candidates:
            updated_actions: list[dict[str, object]] = []
            for raw_action in fresh_plan["actions"]:
                action = dict(raw_action) if isinstance(raw_action, Mapping) else {}
                if action.get("status") == "planned_cleanup":
                    key = (
                        str(action.get("batchId") or ""),
                        str(action.get("version") or ""),
                    )
                    if key in cleaned_quarantine:
                        action["status"] = "quarantine_cleaned"
                    else:
                        action["status"] = "quarantine_cleanup_pending"
                        action["failure"] = pending_cleanup_failures.get(
                            key,
                            "quarantine cleanup did not complete",
                        )
                elif pending_cleanup_failures and action.get("status") == "planned":
                    action["status"] = "not_attempted"
                updated_actions.append(action)
            fresh_plan = {**fresh_plan, "actions": updated_actions}
        if pending_cleanup_failures:
            return {
                **fresh_plan,
                "mode": "apply",
                "ok": False,
                "state": "cleanup_pending",
                "removedGenerationCount": 0,
                "cleanedQuarantineCount": len(cleaned_quarantine),
                "cleanupPendingCount": len(pending_cleanup_failures),
                "failure": (
                    "one or more quarantined generations still require cleanup"
                ),
            }

        for action in candidates:
            _verify_retention_candidate(runtime_root, action)
        if not candidates:
            return {
                **fresh_plan,
                "mode": "apply",
                "ok": True,
                "state": (
                    "cleanup_applied" if cleanup_candidates else "no_changes"
                ),
                "removedGenerationCount": 0,
                "cleanedQuarantineCount": len(cleaned_quarantine),
                "cleanupPendingCount": 0,
            }

        batch_id = uuid.uuid4().hex
        quarantine_root = runtime_root / QUARANTINE_NAME
        if quarantine_root.exists() and (
            quarantine_root.is_symlink() or not quarantine_root.is_dir()
        ):
            raise ManagedPiRuntimeError(
                "managed Pi retention quarantine must be a real directory"
            )
        quarantine_root.mkdir(mode=0o700, exist_ok=True)
        batch_root = quarantine_root / batch_id
        batch_root.mkdir(mode=0o700)
        moved: list[tuple[Path, Path, Mapping[str, object]]] = []
        receipt_path = batch_root / QUARANTINE_RECEIPT_NAME
        failure = ""
        try:
            for action in candidates:
                _verify_retention_candidate(runtime_root, action)
                source = runtime_root / str(action["version"])
                destination = batch_root / str(action["version"])
                _quarantine_retention_candidate(source, destination)
                moved.append((source, destination, action))
            _fsync_directory(runtime_root)
            _fsync_directory(batch_root)
            _write_quarantine_batch_receipt(
                runtime_root,
                batch_root,
                moved,
            )
        except (OSError, ManagedPiRuntimeError) as exc:
            failure = str(exc)
            rollback_failures = _rollback_quarantined_generations(moved)
            if not rollback_failures:
                _discard_quarantine_receipt(receipt_path)
            applied_actions = _retention_failure_actions(
                fresh_plan["actions"],
                moved=moved,
                failed_version=(
                    str(candidates[len(moved)].get("version") or "")
                    if len(moved) < len(candidates)
                    else ""
                ),
                rollback_failures=rollback_failures,
            )
            _remove_empty_quarantine(batch_root, quarantine_root)
            return {
                **fresh_plan,
                "mode": "apply",
                "ok": False,
                "state": (
                    "rollback_incomplete" if rollback_failures else "rolled_back"
                ),
                "removedGenerationCount": 0,
                "cleanedQuarantineCount": len(cleaned_quarantine),
                "cleanupPendingCount": (
                    len(rollback_failures) if rollback_failures else 0
                ),
                "failure": failure,
                "actions": applied_actions,
            }

        pointer_path = runtime_root / POINTER_NAME
        pointer = _read_json_object(pointer_path)
        lifecycle, lifecycle_reason = _pointer_lifecycle(pointer)
        if lifecycle is None:
            rollback_failures = _rollback_quarantined_generations(moved)
            if not rollback_failures:
                _discard_quarantine_receipt(receipt_path)
            _remove_empty_quarantine(batch_root, quarantine_root)
            return {
                **fresh_plan,
                "mode": "apply",
                "ok": False,
                "state": (
                    "rollback_incomplete" if rollback_failures else "rolled_back"
                ),
                "removedGenerationCount": 0,
                "cleanedQuarantineCount": len(cleaned_quarantine),
                "cleanupPendingCount": len(rollback_failures),
                "failure": lifecycle_reason,
                "actions": _retention_failure_actions(
                    fresh_plan["actions"],
                    moved=moved,
                    rollback_failures=rollback_failures,
                ),
            }
        removed_versions = {
            str(action.get("version") or "") for action in candidates
        }
        lifecycle["retiredGenerations"] = [
            entry
            for entry in lifecycle["retiredGenerations"]
            if str(entry.get("version") or "") not in removed_versions
        ]
        updated_pointer = {**pointer, "lifecycle": lifecycle}
        try:
            _write_runtime_pointer(runtime_root, updated_pointer)
        except (OSError, ManagedPiRuntimeError) as exc:
            rollback_failures = _rollback_quarantined_generations(moved)
            if not rollback_failures:
                try:
                    # _atomic_write_bytes replaces current.json before its
                    # parent-directory fsync. If that fsync raises, the new
                    # pointer is already visible even though the write call
                    # reports failure. Restore the exact prior lifecycle after
                    # the candidate directories are back in place.
                    _write_runtime_pointer(runtime_root, pointer)
                except (OSError, ManagedPiRuntimeError) as restore_exc:
                    rollback_failures["runtime_pointer"] = str(restore_exc)
            if not rollback_failures:
                _discard_quarantine_receipt(receipt_path)
            _remove_empty_quarantine(batch_root, quarantine_root)
            return {
                **fresh_plan,
                "mode": "apply",
                "ok": False,
                "state": (
                    "rollback_incomplete" if rollback_failures else "rolled_back"
                ),
                "removedGenerationCount": 0,
                "cleanedQuarantineCount": len(cleaned_quarantine),
                "cleanupPendingCount": len(rollback_failures),
                "failure": str(exc),
                "actions": _retention_failure_actions(
                    fresh_plan["actions"],
                    moved=moved,
                    rollback_failures=rollback_failures,
                ),
            }

        cleanup_failures: dict[str, str] = {}
        removed: set[str] = set()
        for _source, quarantined, action in moved:
            version = str(action.get("version") or "")
            try:
                _verify_quarantined_retention_candidate(
                    runtime_root,
                    batch_root,
                    quarantined,
                    action,
                )
                shutil.rmtree(quarantined)
                removed.add(version)
            except (OSError, ManagedPiRuntimeError) as exc:
                cleanup_failures[version] = str(exc)
        if not cleanup_failures:
            _discard_quarantine_receipt(receipt_path)
        _remove_empty_quarantine(batch_root, quarantine_root)
        applied_actions: list[dict[str, object]] = []
        for raw_action in fresh_plan["actions"]:
            action = dict(raw_action) if isinstance(raw_action, Mapping) else {}
            version = str(action.get("version") or "")
            if action.get("status") == "planned":
                if version in removed:
                    action["status"] = "removed"
                else:
                    action["status"] = "quarantine_cleanup_pending"
                    action["failure"] = cleanup_failures.get(
                        version,
                        "quarantine cleanup did not complete",
                    )
            applied_actions.append(action)
        return {
            **fresh_plan,
            "mode": "apply",
            "ok": not cleanup_failures,
            "state": "cleanup_pending" if cleanup_failures else "applied",
            "removedGenerationCount": len(removed),
            "cleanedQuarantineCount": len(cleaned_quarantine),
            "cleanupPendingCount": len(cleanup_failures),
            "failure": (
                "one or more quarantined generations still require cleanup"
                if cleanup_failures
                else ""
            ),
            "actions": applied_actions,
            "notes": [
                "Retention applied only after an unchanged dry-run plan.",
                (
                    "Candidates were atomically moved to a narrow quarantine before "
                    "the retired lineage was updated."
                ),
                "The current and immediate previous generations were not modified.",
            ],
        }


def read_managed_pi_runtime_retention_plan(path: str | Path) -> dict[str, object]:
    plan_path = Path(path).expanduser()
    if plan_path.is_symlink() or not plan_path.is_file():
        raise ManagedPiRuntimeError("managed Pi retention plan must be a real file")
    return _read_json_object(plan_path)


def read_managed_pi_runtime_acceptance_report(
    path: str | Path,
) -> dict[str, object]:
    report_path = Path(path).expanduser()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(report_path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ManagedPiRuntimeError(
                "managed Pi acceptance receipt must be a real file"
            ) from exc
        raise ManagedPiRuntimeError(
            "managed Pi acceptance receipt is missing"
        ) from exc
    try:
        report_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(report_stat.st_mode)
            or report_stat.st_nlink != 1
        ):
            raise ManagedPiRuntimeError(
                "managed Pi acceptance receipt must be a real file"
            )
        if stat.S_IMODE(report_stat.st_mode) != 0o600:
            raise ManagedPiRuntimeError(
                "managed Pi acceptance receipt must use mode 0600"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, _MAX_JSON_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_JSON_BYTES:
                raise ManagedPiRuntimeError(
                    "managed Pi JSON file is too large: acceptance receipt"
                )
            chunks.append(chunk)
        return _parse_json_object(b"".join(chunks), report_path)
    finally:
        os.close(descriptor)


def write_managed_pi_runtime_retention_report(
    path: str | Path,
    report: Mapping[str, object],
) -> None:
    _atomic_write_bytes(
        Path(path).expanduser(),
        (
            json.dumps(dict(report), ensure_ascii=True, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8"),
        mode=0o600,
    )


def rollback_managed_pi_runtime(
    app_support: str | Path,
    *,
    runtime_version: str,
) -> ManagedPiRuntimeInstallation:
    """Reactivate one digest-bound previously accepted generation."""

    target_version = _safe_version(
        runtime_version,
        label="rollback runtime version",
    )
    with _managed_runtime_lock(
        app_support,
        create_runtime_root=False,
    ) as runtime_root:
        pointer = _read_json_object(runtime_root / POINTER_NAME)
        lifecycle, lifecycle_reason = _pointer_lifecycle(pointer)
        if lifecycle is None:
            raise ManagedPiRuntimeError(
                f"managed Pi rollback has no accepted lifecycle: {lifecycle_reason}"
            )
        accepted = lifecycle["acceptedGeneration"]
        if (
            isinstance(accepted, Mapping)
            and str(accepted.get("version") or "") == target_version
        ):
            # A successful rollback can lose its caller response. Retrying the
            # same request must prove and return the already-active target,
            # rather than fail merely because it moved from previous/retired
            # lineage into acceptedGeneration.
            return _discover_managed_pi_runtime_locked(runtime_root)
        candidates = [
            lifecycle.get("previousAcceptedGeneration"),
            *lifecycle["retiredGenerations"],
        ]
        target_entry = next(
            (
                entry
                for entry in candidates
                if isinstance(entry, Mapping)
                and str(entry.get("version") or "") == target_version
            ),
            None,
        )
        if not isinstance(target_entry, Mapping):
            raise ManagedPiRuntimeError(
                "managed Pi rollback target is not in the accepted lineage"
            )
        target = _load_installation(
            runtime_root=runtime_root,
            runtime_dir=runtime_root / target_version,
            expected_manifest_sha256=_sha256_text(
                target_entry.get("manifestSha256"),
                label="rollback runtime manifest digest",
            ),
            expected_pi_version="",
            verify_all_files=True,
        )
        _activate_preaccepted_managed_pi_runtime_locked(
            runtime_root,
            target,
            accepted_entry=dict(target_entry),
        )
        return _discover_managed_pi_runtime_locked(runtime_root)


def _activate_managed_pi_runtime_locked(
    runtime_root: Path,
    installation: ManagedPiRuntimeInstallation,
    *,
    acceptance: Mapping[str, object] | None,
) -> None:
    _require_supported_protocol(installation.protocol_version)
    if acceptance is None:
        raise ManagedPiRuntimeError(
            "managed Pi protocol v2 activation requires a passed acceptance receipt"
        )
    accepted_entry = _accepted_generation_entry(installation, acceptance)
    _activate_preaccepted_managed_pi_runtime_locked(
        runtime_root, installation, accepted_entry=accepted_entry,
    )


def _activate_preaccepted_managed_pi_runtime_locked(
    runtime_root: Path,
    installation: ManagedPiRuntimeInstallation,
    *,
    accepted_entry: dict[str, object],
) -> None:
    _require_supported_protocol(installation.protocol_version)
    pointer_path = runtime_root / POINTER_NAME
    prior_pointer: dict[str, object] | None = None
    prior_current: ManagedPiRuntimeInstallation | None = None
    prior_previous: ManagedPiRuntimeInstallation | None = None
    prior_lifecycle: dict[str, object] | None = None
    if pointer_path.is_file() and not pointer_path.is_symlink():
        try:
            prior_pointer = _read_json_object(pointer_path)
            if prior_pointer.get("schemaVersion") != POINTER_SCHEMA_VERSION:
                raise ManagedPiRuntimeError(
                    "managed Pi runtime pointer schema is unsupported"
                )
            prior_current = _load_installation(
                runtime_root=runtime_root,
                runtime_dir=runtime_root
                / _safe_version(
                    prior_pointer.get("version"),
                    label="runtime pointer version",
                ),
                expected_manifest_sha256=_sha256_text(
                    prior_pointer.get("manifestSha256"),
                    label="runtime manifest digest",
                ),
                expected_pi_version="",
                verify_all_files=True,
            )
            prior_previous, _ = _pointer_previous_installation(
                runtime_root,
                prior_pointer,
            )
            prior_lifecycle, _ = _pointer_lifecycle(prior_pointer)
        except (OSError, ManagedPiRuntimeError):
            # A malformed or legacy pointer remains runnable only through its
            # active fields. It cannot authorize retirement of unknown data.
            prior_pointer = None
            prior_current = None
            prior_previous = None
            prior_lifecycle = None

    same_generation = bool(
        prior_current is not None
        and prior_current.runtime_version == installation.runtime_version
        and prior_current.manifest_sha256 == installation.manifest_sha256
    )
    previous_installation = prior_previous if same_generation else prior_current

    retired = [
        dict(item)
        for item in (
            prior_lifecycle.get("retiredGenerations", [])
            if prior_lifecycle is not None
            else []
        )
        if isinstance(item, Mapping)
    ]
    retired = [
        item
        for item in retired
        if str(item.get("version") or "") != installation.runtime_version
    ]

    prior_accepted = (
        dict(prior_lifecycle["acceptedGeneration"])
        if prior_lifecycle is not None
        and isinstance(prior_lifecycle.get("acceptedGeneration"), Mapping)
        else None
    )
    prior_previous_accepted = (
        dict(prior_lifecycle["previousAcceptedGeneration"])
        if prior_lifecycle is not None
        and isinstance(prior_lifecycle.get("previousAcceptedGeneration"), Mapping)
        else None
    )
    if (
        not same_generation
        and prior_previous_accepted is not None
        and str(prior_previous_accepted.get("version") or "")
        != installation.runtime_version
    ):
        retired.append(
            {
                **prior_previous_accepted,
                "retiredAtMs": int(time.time() * 1000),
            }
        )
    retired = _bounded_retired_generations(retired)

    previous_accepted = (
        prior_previous_accepted if same_generation else prior_accepted
    )
    lifecycle: dict[str, object] = {
        "schemaVersion": LIFECYCLE_SCHEMA_VERSION,
        "acceptedGeneration": accepted_entry,
        "previousAcceptedGeneration": previous_accepted,
        "retiredGenerations": retired,
    }
    pointer: dict[str, object] = {
        "schemaVersion": POINTER_SCHEMA_VERSION,
        "version": installation.runtime_version,
        "manifestSha256": installation.manifest_sha256,
        "activatedAtMs": int(time.time() * 1000),
        "lifecycle": lifecycle,
    }
    if (
        previous_installation is not None
        and previous_installation.runtime_version != installation.runtime_version
    ):
        pointer["previousVersion"] = previous_installation.runtime_version
        pointer["previousManifestSha256"] = previous_installation.manifest_sha256
    _write_runtime_pointer(runtime_root, pointer)


def _activation_predecessor(
    runtime_root: Path,
    *,
    activating_version: str,
) -> ManagedPiRuntimeInstallation | None:
    pointer_path = runtime_root / POINTER_NAME
    if not pointer_path.is_file() or pointer_path.is_symlink():
        return None
    try:
        pointer = _read_json_object(pointer_path)
        if pointer.get("schemaVersion") != POINTER_SCHEMA_VERSION:
            return None
        current_version = _safe_version(
            pointer.get("version"),
            label="runtime pointer version",
        )
        current_digest = _sha256_text(
            pointer.get("manifestSha256"),
            label="runtime manifest digest",
        )
        current = _load_installation(
            runtime_root=runtime_root,
            runtime_dir=runtime_root / current_version,
            expected_manifest_sha256=current_digest,
            expected_pi_version="",
            verify_all_files=True,
        )
        if current.runtime_version != activating_version:
            return current
        previous, _ = _pointer_previous_installation(runtime_root, pointer)
        return previous
    except (OSError, ManagedPiRuntimeError):
        # A damaged legacy pointer must not block installation, but it cannot
        # authorize later deletion of any prior generation.
        return None


def _accepted_generation_entry(
    installation: ManagedPiRuntimeInstallation,
    acceptance: Mapping[str, object],
) -> dict[str, object]:
    if acceptance.get("schemaVersion") != ACCEPTANCE_SCHEMA_VERSION:
        raise ManagedPiRuntimeError(
            "managed Pi acceptance receipt schema is unsupported"
        )
    if acceptance.get("status") != "passed_not_installed":
        raise ManagedPiRuntimeError("managed Pi acceptance receipt did not pass")
    if acceptance.get("runtimeVersion") != installation.runtime_version:
        raise ManagedPiRuntimeError(
            "managed Pi acceptance receipt targets another runtime version"
        )
    if acceptance.get("manifestSha256") != installation.manifest_sha256:
        raise ManagedPiRuntimeError(
            "managed Pi acceptance receipt targets another runtime manifest"
        )
    if (
        str(acceptance.get("protocolVersion") or "")
        != installation.protocol_version
        or installation.protocol_version != "2"
    ):
        raise ManagedPiRuntimeError(
            "managed Pi acceptance receipt did not exercise protocol v2"
        )
    methods = acceptance.get("verifiedMethods")
    if (
        not isinstance(methods, list)
        or not set(REQUIRED_ACCEPTANCE_METHODS).issubset(
            {str(item) for item in methods}
        )
    ):
        raise ManagedPiRuntimeError(
            "managed Pi acceptance receipt omits required lifecycle methods"
        )
    if acceptance.get("productionEnabled") is not False:
        raise ManagedPiRuntimeError(
            "managed Pi acceptance must use the offline deterministic adapter"
        )
    manifest = _read_json_object(installation.runtime_dir / MANIFEST_NAME)
    source = manifest.get("source")
    expected_source_commit = (
        str(source.get("commit") or "")
        if isinstance(source, Mapping)
        else ""
    )
    if (
        not expected_source_commit
        or acceptance.get("sourceCommit") != expected_source_commit
    ):
        raise ManagedPiRuntimeError(
            "managed Pi acceptance receipt targets another source commit"
        )
    if acceptance.get("promptDelivery") != "prompt":
        raise ManagedPiRuntimeError(
            "managed Pi acceptance receipt did not prove Session prompt delivery"
        )
    if acceptance.get("steerDelivery") != "steer":
        raise ManagedPiRuntimeError(
            "managed Pi acceptance receipt did not prove Session steer delivery"
        )
    if acceptance.get("abortAcknowledged") is not True:
        raise ManagedPiRuntimeError(
            "managed Pi acceptance receipt did not prove Session abort"
        )
    if acceptance.get("debugContextAvailable") is not True:
        raise ManagedPiRuntimeError(
            "managed Pi acceptance receipt did not expose Provider context"
        )
    if acceptance.get("terminalSessionIdle") is not True:
        raise ManagedPiRuntimeError(
            "managed Pi acceptance receipt did not settle the Session"
        )
    return {
        "version": installation.runtime_version,
        "manifestSha256": installation.manifest_sha256,
        "acceptanceReceiptSha256": _canonical_json_sha256(acceptance),
        "acceptedAtMs": int(time.time() * 1000),
    }


def _pointer_lifecycle(
    pointer: Mapping[str, object],
) -> tuple[dict[str, object] | None, str]:
    raw = pointer.get("lifecycle")
    if raw is None:
        return None, "active pointer has no accepted lifecycle lineage"
    if not isinstance(raw, Mapping):
        return None, "active pointer lifecycle is invalid"
    try:
        if raw.get("schemaVersion") != LIFECYCLE_SCHEMA_VERSION:
            raise ManagedPiRuntimeError(
                "active pointer lifecycle schema is unsupported"
            )
        accepted = _lifecycle_generation(
            raw.get("acceptedGeneration"),
            label="accepted",
        )
        active_version = _safe_version(
            pointer.get("version"),
            label="runtime pointer version",
        )
        active_digest = _sha256_text(
            pointer.get("manifestSha256"),
            label="runtime manifest digest",
        )
        if (
            accepted["version"] != active_version
            or accepted["manifestSha256"] != active_digest
        ):
            raise ManagedPiRuntimeError(
                "accepted lifecycle does not match the active pointer"
            )
        raw_previous = raw.get("previousAcceptedGeneration")
        previous = (
            None
            if raw_previous is None
            else _lifecycle_generation(raw_previous, label="previous accepted")
        )
        if previous is not None:
            if (
                previous["version"] != str(pointer.get("previousVersion") or "")
                or previous["manifestSha256"]
                != str(pointer.get("previousManifestSha256") or "")
            ):
                raise ManagedPiRuntimeError(
                    "previous accepted lifecycle does not match the rollback pointer"
                )
        retired_raw = raw.get("retiredGenerations")
        if (
            not isinstance(retired_raw, list)
            or len(retired_raw) > _MAX_RETIRED_GENERATIONS
        ):
            raise ManagedPiRuntimeError("retired lifecycle list is invalid")
        retired = [
            _lifecycle_generation(
                item,
                label="retired",
                require_retired_at=True,
            )
            for item in retired_raw
        ]
        versions = [str(item["version"]) for item in retired]
        protected = {str(accepted["version"])}
        if previous is not None:
            protected.add(str(previous["version"]))
        if (
            len(set(versions)) != len(versions)
            or any(version in protected for version in versions)
        ):
            raise ManagedPiRuntimeError(
                "retired lifecycle contains duplicate or protected generations"
            )
    except ManagedPiRuntimeError as exc:
        return None, str(exc)
    return {
        "schemaVersion": LIFECYCLE_SCHEMA_VERSION,
        "acceptedGeneration": accepted,
        "previousAcceptedGeneration": previous,
        "retiredGenerations": retired,
    }, ""


def _lifecycle_generation(
    value: object,
    *,
    label: str,
    require_retired_at: bool = False,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ManagedPiRuntimeError(f"{label} lifecycle generation is invalid")
    accepted_at = value.get("acceptedAtMs")
    if (
        not isinstance(accepted_at, int)
        or isinstance(accepted_at, bool)
        or accepted_at < 0
    ):
        raise ManagedPiRuntimeError(
            f"{label} lifecycle accepted timestamp is invalid"
        )
    normalized: dict[str, object] = {
        "version": _safe_version(
            value.get("version"),
            label=f"{label} lifecycle version",
        ),
        "manifestSha256": _sha256_text(
            value.get("manifestSha256"),
            label=f"{label} lifecycle manifest digest",
        ),
        "acceptanceReceiptSha256": _sha256_text(
            value.get("acceptanceReceiptSha256"),
            label=f"{label} lifecycle acceptance digest",
        ),
        "acceptedAtMs": accepted_at,
    }
    if require_retired_at:
        retired_at = value.get("retiredAtMs")
        if (
            not isinstance(retired_at, int)
            or isinstance(retired_at, bool)
            or retired_at < 0
        ):
            raise ManagedPiRuntimeError(
                f"{label} lifecycle retired timestamp is invalid"
            )
        normalized["retiredAtMs"] = retired_at
    return normalized


def _bounded_retired_generations(
    entries: list[dict[str, object]],
) -> list[dict[str, object]]:
    by_version: dict[str, dict[str, object]] = {}
    order: list[str] = []
    for entry in entries:
        version = str(entry.get("version") or "")
        if version in by_version:
            order.remove(version)
        by_version[version] = _lifecycle_generation(
            entry,
            label="retired",
            require_retired_at=True,
        )
        order.append(version)
    return [
        by_version[version]
        for version in order[-_MAX_RETIRED_GENERATIONS:]
    ]


def _write_runtime_pointer(
    runtime_root: Path,
    pointer: Mapping[str, object],
) -> None:
    payload = dict(pointer)
    if payload.get("schemaVersion") != POINTER_SCHEMA_VERSION:
        raise ManagedPiRuntimeError("managed Pi runtime pointer schema is unsupported")
    _safe_version(payload.get("version"), label="runtime pointer version")
    _sha256_text(payload.get("manifestSha256"), label="runtime manifest digest")
    if "lifecycle" in payload:
        lifecycle, reason = _pointer_lifecycle(payload)
        if lifecycle is None:
            raise ManagedPiRuntimeError(reason)
        payload["lifecycle"] = lifecycle
    _atomic_write_bytes(
        runtime_root / POINTER_NAME,
        (
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8"),
        mode=0o600,
    )


def _pointer_previous_installation(
    runtime_root: Path,
    pointer: Mapping[str, object],
) -> tuple[ManagedPiRuntimeInstallation | None, str]:
    raw_version = str(pointer.get("previousVersion") or "").strip()
    raw_digest = str(pointer.get("previousManifestSha256") or "").strip()
    if not raw_version and not raw_digest:
        return None, "active pointer does not record a verified immediate predecessor"
    if not raw_version or not raw_digest:
        return None, "active pointer has incomplete immediate predecessor metadata"
    try:
        active_version = _safe_version(
            pointer.get("version"),
            label="runtime pointer version",
        )
        previous_version = _safe_version(
            raw_version,
            label="previous runtime pointer version",
        )
        if previous_version == active_version:
            raise ManagedPiRuntimeError(
                "active and previous managed Pi generations must be different"
            )
        previous_digest = _sha256_text(
            raw_digest,
            label="previous runtime manifest digest",
        )
        previous = _load_installation(
            runtime_root=runtime_root,
            runtime_dir=runtime_root / previous_version,
            expected_manifest_sha256=previous_digest,
            expected_pi_version="",
            verify_all_files=True,
        )
    except (OSError, ManagedPiRuntimeError) as exc:
        return None, f"immediate predecessor is not verified: {exc}"
    return previous, ""


def _verified_generation(
    runtime_root: Path,
    runtime_dir: Path,
) -> ManagedPiRuntimeInstallation:
    manifest_path = runtime_dir / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ManagedPiRuntimeError("managed Pi runtime manifest is missing")
    manifest_digest = hashlib.sha256(_read_limited_bytes(manifest_path)).hexdigest()
    return _load_installation(
        runtime_root=runtime_root,
        runtime_dir=runtime_dir,
        expected_manifest_sha256=manifest_digest,
        expected_pi_version="",
        verify_all_files=True,
    )


def _generation_receipt(
    installation: ManagedPiRuntimeInstallation,
) -> dict[str, object]:
    return {
        "version": installation.runtime_version,
        "manifestSha256": installation.manifest_sha256,
        "runtimeDir": str(installation.runtime_dir),
    }


def _pending_quarantine_cleanup_actions(
    runtime_root: Path,
    *,
    protected_versions: set[str],
    retired_versions: set[str],
) -> list[dict[str, object]]:
    quarantine_root = runtime_root / QUARANTINE_NAME
    if not quarantine_root.exists():
        return []
    if quarantine_root.is_symlink() or not quarantine_root.is_dir():
        return [
            {
                "kind": "preserve_entry",
                "target": str(quarantine_root),
                "status": "preserved_quarantine_invalid",
                "reason": "retention quarantine is not a real directory",
            }
        ]

    actions: list[dict[str, object]] = []
    for batch_root in sorted(quarantine_root.iterdir(), key=lambda item: item.name):
        if (
            batch_root.is_symlink()
            or not batch_root.is_dir()
            or not _RETENTION_BATCH_PATTERN.fullmatch(batch_root.name)
        ):
            actions.append(
                {
                    "kind": "preserve_entry",
                    "target": str(batch_root),
                    "status": "preserved_quarantine_invalid",
                    "reason": "retention quarantine batch is not trusted",
                }
            )
            continue
        try:
            receipt, receipt_digest = _read_quarantine_batch_receipt(
                runtime_root,
                batch_root,
            )
        except (OSError, ManagedPiRuntimeError) as exc:
            actions.append(
                {
                    "kind": "preserve_entry",
                    "target": str(batch_root),
                    "status": "preserved_quarantine_unverified",
                    "reason": str(exc),
                }
            )
            continue

        entries = receipt["generations"]
        expected_names = {
            QUARANTINE_RECEIPT_NAME,
            *(
                str(entry["version"])
                for entry in entries
                if isinstance(entry, Mapping)
            ),
        }
        actual_names = {entry.name for entry in batch_root.iterdir()}
        unknown_names = actual_names - expected_names
        if unknown_names:
            actions.append(
                {
                    "kind": "preserve_entry",
                    "target": str(batch_root),
                    "status": "preserved_quarantine_unverified",
                    "reason": (
                        "retention quarantine batch contains an unowned entry: "
                        f"{sorted(unknown_names)[0]}"
                    ),
                }
            )

        existing_generation_count = 0
        for raw_entry in entries:
            entry = dict(raw_entry)
            version = str(entry["version"])
            target = batch_root / version
            source = runtime_root / version
            if not target.exists() and not target.is_symlink():
                continue
            existing_generation_count += 1
            if version in protected_versions:
                actions.append(
                    {
                        **entry,
                        "kind": "preserve_generation",
                        "target": str(target),
                        "batchId": batch_root.name,
                        "status": "preserved_quarantine_protected",
                        "reason": (
                            "quarantined generation is now active or the immediate "
                            "rollback predecessor"
                        ),
                    }
                )
                continue
            if version in retired_versions:
                actions.append(
                    {
                        **entry,
                        "kind": "preserve_generation",
                        "target": str(target),
                        "batchId": batch_root.name,
                        "status": "preserved_quarantine_retired",
                        "reason": (
                            "pointer rollback left the generation in accepted retired "
                            "lineage; it must not be deleted"
                        ),
                    }
                )
                continue
            try:
                target_stat = target.stat(follow_symlinks=False)
                if (
                    target.is_symlink()
                    or not stat.S_ISDIR(target_stat.st_mode)
                    or target_stat.st_dev != entry["directoryDevice"]
                    or target_stat.st_ino != entry["directoryInode"]
                    or source.exists()
                    or source.is_symlink()
                ):
                    raise ManagedPiRuntimeError(
                        "quarantined generation identity is no longer exclusive"
                    )
            except (OSError, ManagedPiRuntimeError) as exc:
                actions.append(
                    {
                        **entry,
                        "kind": "preserve_generation",
                        "target": str(target),
                        "batchId": batch_root.name,
                        "status": "preserved_quarantine_unverified",
                        "reason": str(exc),
                    }
                )
                continue
            actions.append(
                {
                    **entry,
                    "kind": "cleanup_quarantine_generation",
                    "target": str(target),
                    "sourceTarget": str(source),
                    "batchId": batch_root.name,
                    "receiptPath": str(batch_root / QUARANTINE_RECEIPT_NAME),
                    "receiptSha256": receipt_digest,
                    "status": "planned_cleanup",
                    "reason": (
                        "a prior accepted retention batch updated the pointer but did "
                        "not finish deleting this inode-bound quarantine target"
                    ),
                }
            )

        if (
            existing_generation_count == 0
            and not unknown_names
            and actual_names == {QUARANTINE_RECEIPT_NAME}
        ):
            actions.append(
                {
                    "kind": "cleanup_quarantine_batch",
                    "target": str(batch_root),
                    "batchId": batch_root.name,
                    "receiptPath": str(batch_root / QUARANTINE_RECEIPT_NAME),
                    "receiptSha256": receipt_digest,
                    "status": "planned_cleanup",
                    "reason": (
                        "all inode-bound targets are gone and only the durable batch "
                        "receipt remains"
                    ),
                }
            )
    return actions


def _write_quarantine_batch_receipt(
    runtime_root: Path,
    batch_root: Path,
    moved: list[tuple[Path, Path, Mapping[str, object]]],
) -> None:
    generations = [
        {
            "version": str(action["version"]),
            "manifestSha256": str(action["manifestSha256"]),
            "sourceTarget": str(source),
            "quarantineTarget": str(quarantined),
            "directoryDevice": int(action["directoryDevice"]),
            "directoryInode": int(action["directoryInode"]),
        }
        for source, quarantined, action in moved
    ]
    receipt = {
        "schemaVersion": QUARANTINE_RECEIPT_SCHEMA_VERSION,
        "batchId": batch_root.name,
        "runtimeRoot": str(runtime_root),
        "generations": generations,
    }
    _validate_quarantine_batch_receipt(runtime_root, batch_root, receipt)
    _atomic_write_bytes(
        batch_root / QUARANTINE_RECEIPT_NAME,
        (
            json.dumps(receipt, ensure_ascii=True, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8"),
        mode=0o600,
    )


def _read_quarantine_batch_receipt(
    runtime_root: Path,
    batch_root: Path,
) -> tuple[dict[str, object], str]:
    quarantine_root = runtime_root / QUARANTINE_NAME
    if (
        batch_root.parent != quarantine_root
        or not _RETENTION_BATCH_PATTERN.fullmatch(batch_root.name)
        or batch_root.is_symlink()
        or not batch_root.is_dir()
    ):
        raise ManagedPiRuntimeError(
            "managed Pi retention quarantine batch is invalid"
        )
    receipt_path = batch_root / QUARANTINE_RECEIPT_NAME
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(receipt_path, flags)
    except OSError as exc:
        raise ManagedPiRuntimeError(
            "managed Pi retention quarantine receipt is missing"
        ) from exc
    try:
        receipt_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(receipt_stat.st_mode)
            or receipt_stat.st_nlink != 1
            or stat.S_IMODE(receipt_stat.st_mode) != 0o600
        ):
            raise ManagedPiRuntimeError(
                "managed Pi retention quarantine receipt must be a 0600 real file"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(64 * 1024, _MAX_JSON_BYTES + 1 - total),
            )
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_JSON_BYTES:
                raise ManagedPiRuntimeError(
                    "managed Pi retention quarantine receipt is too large"
                )
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    receipt = _parse_json_object(data, receipt_path)
    _validate_quarantine_batch_receipt(runtime_root, batch_root, receipt)
    return receipt, hashlib.sha256(data).hexdigest()


def _validate_quarantine_batch_receipt(
    runtime_root: Path,
    batch_root: Path,
    receipt: Mapping[str, object],
) -> None:
    if (
        receipt.get("schemaVersion") != QUARANTINE_RECEIPT_SCHEMA_VERSION
        or receipt.get("batchId") != batch_root.name
        or receipt.get("runtimeRoot") != str(runtime_root)
    ):
        raise ManagedPiRuntimeError(
            "managed Pi retention quarantine receipt identity is invalid"
        )
    raw_generations = receipt.get("generations")
    if (
        not isinstance(raw_generations, list)
        or not raw_generations
        or len(raw_generations) > _MAX_RETIRED_GENERATIONS
        or any(not isinstance(entry, Mapping) for entry in raw_generations)
    ):
        raise ManagedPiRuntimeError(
            "managed Pi retention quarantine receipt generations are invalid"
        )
    seen: set[str] = set()
    for raw_entry in raw_generations:
        entry = raw_entry if isinstance(raw_entry, Mapping) else {}
        version = _safe_version(
            entry.get("version"),
            label="quarantined runtime version",
        )
        if version in seen:
            raise ManagedPiRuntimeError(
                "managed Pi retention quarantine receipt repeats a generation"
            )
        seen.add(version)
        _sha256_text(
            entry.get("manifestSha256"),
            label="quarantined runtime manifest digest",
        )
        if (
            entry.get("sourceTarget") != str(runtime_root / version)
            or entry.get("quarantineTarget") != str(batch_root / version)
        ):
            raise ManagedPiRuntimeError(
                "managed Pi retention quarantine receipt path is invalid"
            )
        for field in ("directoryDevice", "directoryInode"):
            value = entry.get(field)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise ManagedPiRuntimeError(
                    "managed Pi retention quarantine receipt identity is invalid"
                )


def _apply_pending_quarantine_cleanup(
    runtime_root: Path,
    action: Mapping[str, object],
) -> None:
    batch_id = str(action.get("batchId") or "")
    if not _RETENTION_BATCH_PATTERN.fullmatch(batch_id):
        raise ManagedPiRuntimeError(
            "managed Pi retention cleanup batch is invalid"
        )
    batch_root = runtime_root / QUARANTINE_NAME / batch_id
    receipt, receipt_digest = _read_quarantine_batch_receipt(
        runtime_root,
        batch_root,
    )
    if (
        action.get("receiptPath")
        != str(batch_root / QUARANTINE_RECEIPT_NAME)
        or action.get("receiptSha256") != receipt_digest
    ):
        raise ManagedPiRuntimeError(
            "managed Pi retention cleanup receipt changed after dry-run"
        )
    kind = action.get("kind")
    if kind == "cleanup_quarantine_batch":
        if {entry.name for entry in batch_root.iterdir()} != {
            QUARANTINE_RECEIPT_NAME
        }:
            raise ManagedPiRuntimeError(
                "managed Pi retention quarantine batch is no longer empty"
            )
        _finalize_quarantine_batch(runtime_root, batch_root)
        return
    if kind != "cleanup_quarantine_generation":
        raise ManagedPiRuntimeError(
            "managed Pi retention cleanup action is unsupported"
        )
    version = _safe_version(
        action.get("version"),
        label="retention cleanup generation version",
    )
    matching = [
        entry
        for entry in receipt["generations"]
        if isinstance(entry, Mapping) and entry.get("version") == version
    ]
    if len(matching) != 1:
        raise ManagedPiRuntimeError(
            "managed Pi retention cleanup generation is absent from its receipt"
        )
    entry = matching[0]
    target = batch_root / version
    source = runtime_root / version
    if (
        action.get("target") != str(target)
        or action.get("sourceTarget") != str(source)
        or action.get("manifestSha256") != entry.get("manifestSha256")
        or action.get("directoryDevice") != entry.get("directoryDevice")
        or action.get("directoryInode") != entry.get("directoryInode")
        or source.exists()
        or source.is_symlink()
    ):
        raise ManagedPiRuntimeError(
            "managed Pi retention cleanup identity changed after dry-run"
        )
    target_stat = target.stat(follow_symlinks=False)
    if (
        target.is_symlink()
        or not stat.S_ISDIR(target_stat.st_mode)
        or target_stat.st_dev != entry.get("directoryDevice")
        or target_stat.st_ino != entry.get("directoryInode")
    ):
        raise ManagedPiRuntimeError(
            "managed Pi retention cleanup target identity changed"
        )
    shutil.rmtree(target)
    _finalize_quarantine_batch_if_empty(runtime_root, batch_root)


def _discard_quarantine_receipt(receipt_path: Path) -> None:
    receipt_path.unlink(missing_ok=True)
    try:
        _fsync_directory(receipt_path.parent)
    except OSError:
        # The logical cleanup already completed. A later scan can remove an
        # empty batch if the directory entry is not durable across a crash.
        pass


def _finalize_quarantine_batch_if_empty(
    runtime_root: Path,
    batch_root: Path,
) -> None:
    remaining = {
        entry.name
        for entry in batch_root.iterdir()
        if entry.name != QUARANTINE_RECEIPT_NAME
    }
    if not remaining:
        _finalize_quarantine_batch(runtime_root, batch_root)


def _finalize_quarantine_batch(
    runtime_root: Path,
    batch_root: Path,
) -> None:
    _discard_quarantine_receipt(batch_root / QUARANTINE_RECEIPT_NAME)
    batch_root.rmdir()
    quarantine_root = runtime_root / QUARANTINE_NAME
    try:
        quarantine_root.rmdir()
    except OSError:
        pass


def _retention_plan_signature(
    plan: Mapping[str, object],
    *,
    app_support: str | Path,
) -> tuple[object, ...]:
    if plan.get("schemaVersion") != RETENTION_SCHEMA_VERSION:
        raise ManagedPiRuntimeError("managed Pi retention plan schema is unsupported")
    if plan.get("mode") != "dry-run":
        raise ManagedPiRuntimeError("managed Pi retention apply requires a dry-run plan")
    retain_generations = plan.get("retainGenerations")
    if (
        not isinstance(retain_generations, int)
        or isinstance(retain_generations, bool)
        or retain_generations != 2
    ):
        raise ManagedPiRuntimeError(
            "managed Pi retention plan must retain exactly two generations"
        )
    expected_app_support = Path(app_support).expanduser().resolve(strict=False)
    if str(plan.get("appSupport") or "") != str(expected_app_support):
        raise ManagedPiRuntimeError(
            "managed Pi retention plan targets a different application support directory"
        )
    runtime_root = _runtime_root(expected_app_support)
    if str(plan.get("runtimeRoot") or "") != str(runtime_root):
        raise ManagedPiRuntimeError(
            "managed Pi retention plan targets a different runtime root"
        )

    active = _generation_signature(plan.get("activeGeneration"), label="active")
    raw_previous = plan.get("previousGeneration")
    previous = (
        None
        if raw_previous is None
        else _generation_signature(raw_previous, label="previous")
    )
    if previous is not None and previous[0] == active[0]:
        raise ManagedPiRuntimeError(
            "managed Pi retention plan protects the same generation twice"
        )
    lifecycle_digest_text = str(plan.get("lifecycleSha256") or "")
    lifecycle_digest = (
        ""
        if not lifecycle_digest_text
        else _sha256_text(
            lifecycle_digest_text,
            label="retention lifecycle digest",
        )
    )
    retired_lineage_count = plan.get("retiredLineageCount")
    if (
        not isinstance(retired_lineage_count, int)
        or isinstance(retired_lineage_count, bool)
        or retired_lineage_count < 0
        or retired_lineage_count > _MAX_RETIRED_GENERATIONS
    ):
        raise ManagedPiRuntimeError(
            "managed Pi retention retired lineage count is invalid"
        )

    raw_actions = plan.get("actions")
    if not isinstance(raw_actions, list):
        raise ManagedPiRuntimeError("managed Pi retention plan actions are invalid")
    candidates: list[tuple[object, ...]] = []
    cleanup_candidates: list[tuple[object, ...]] = []
    protected_versions = {active[0]}
    if previous is not None:
        protected_versions.add(previous[0])
    for raw_action in raw_actions:
        if not isinstance(raw_action, Mapping):
            continue
        status = raw_action.get("status")
        if status == "planned_cleanup":
            kind = raw_action.get("kind")
            if kind not in {
                "cleanup_quarantine_generation",
                "cleanup_quarantine_batch",
            }:
                raise ManagedPiRuntimeError(
                    "managed Pi retention plan contains an unsupported cleanup action"
                )
            batch_id = str(raw_action.get("batchId") or "")
            if not _RETENTION_BATCH_PATTERN.fullmatch(batch_id):
                raise ManagedPiRuntimeError(
                    "managed Pi retention cleanup batch is invalid"
                )
            batch_root = runtime_root / QUARANTINE_NAME / batch_id
            receipt_path = batch_root / QUARANTINE_RECEIPT_NAME
            if raw_action.get("receiptPath") != str(receipt_path):
                raise ManagedPiRuntimeError(
                    "managed Pi retention cleanup receipt path is invalid"
                )
            receipt_digest = _sha256_text(
                raw_action.get("receiptSha256"),
                label="retention cleanup receipt digest",
            )
            if kind == "cleanup_quarantine_batch":
                if raw_action.get("target") != str(batch_root):
                    raise ManagedPiRuntimeError(
                        "managed Pi retention cleanup batch path is invalid"
                    )
                cleanup_candidates.append(
                    (kind, batch_id, "", receipt_digest, 0, 0)
                )
                continue
            version = _safe_version(
                raw_action.get("version"),
                label="retention cleanup generation version",
            )
            if version in protected_versions:
                raise ManagedPiRuntimeError(
                    "managed Pi retention plan attempts to clean a protected generation"
                )
            manifest_digest = _sha256_text(
                raw_action.get("manifestSha256"),
                label="retention cleanup manifest digest",
            )
            target = batch_root / version
            if (
                raw_action.get("target") != str(target)
                or raw_action.get("sourceTarget") != str(runtime_root / version)
            ):
                raise ManagedPiRuntimeError(
                    "managed Pi retention cleanup path is invalid"
                )
            device = raw_action.get("directoryDevice")
            inode = raw_action.get("directoryInode")
            if (
                not isinstance(device, int)
                or isinstance(device, bool)
                or device < 0
                or not isinstance(inode, int)
                or isinstance(inode, bool)
                or inode < 0
            ):
                raise ManagedPiRuntimeError(
                    "managed Pi retention cleanup identity is invalid"
                )
            cleanup_candidates.append(
                (
                    kind,
                    batch_id,
                    version,
                    receipt_digest,
                    manifest_digest,
                    device,
                    inode,
                )
            )
            continue
        if status != "planned":
            continue
        if raw_action.get("kind") != "remove_generation":
            raise ManagedPiRuntimeError(
                "managed Pi retention plan contains an unsupported removal action"
            )
        version = _safe_version(
            raw_action.get("version"),
            label="retention candidate version",
        )
        if version in protected_versions:
            raise ManagedPiRuntimeError(
                "managed Pi retention plan attempts to remove a protected generation"
            )
        manifest_digest = _sha256_text(
            raw_action.get("manifestSha256"),
            label="retention candidate manifest digest",
        )
        target = runtime_root / version
        if str(raw_action.get("target") or "") != str(target):
            raise ManagedPiRuntimeError(
                "managed Pi retention candidate path is outside its generation"
            )
        device = raw_action.get("directoryDevice")
        inode = raw_action.get("directoryInode")
        if (
            not isinstance(device, int)
            or isinstance(device, bool)
            or device < 0
            or not isinstance(inode, int)
            or isinstance(inode, bool)
            or inode < 0
        ):
            raise ManagedPiRuntimeError(
                "managed Pi retention candidate identity is invalid"
            )
        candidates.append((version, manifest_digest, device, inode))
    candidates.sort()
    cleanup_candidates.sort()
    planned_count = plan.get("plannedRemovalCount")
    if (
        not isinstance(planned_count, int)
        or isinstance(planned_count, bool)
        or planned_count != len(candidates)
    ):
        raise ManagedPiRuntimeError(
            "managed Pi retention planned removal count is inconsistent"
        )
    planned_cleanup_count = plan.get("plannedCleanupCount")
    if (
        not isinstance(planned_cleanup_count, int)
        or isinstance(planned_cleanup_count, bool)
        or planned_cleanup_count != len(cleanup_candidates)
    ):
        raise ManagedPiRuntimeError(
            "managed Pi retention planned cleanup count is inconsistent"
        )
    if candidates and previous is None:
        raise ManagedPiRuntimeError(
            "managed Pi retention plan has removals without a verified predecessor"
        )
    if candidates and not lifecycle_digest:
        raise ManagedPiRuntimeError(
            "managed Pi retention plan has removals without accepted lineage"
        )
    return (
        str(expected_app_support),
        str(runtime_root),
        retain_generations,
        active,
        previous,
        lifecycle_digest,
        retired_lineage_count,
        tuple(candidates),
        tuple(cleanup_candidates),
    )


def _generation_signature(
    value: object,
    *,
    label: str,
) -> tuple[str, str, str]:
    if not isinstance(value, Mapping):
        raise ManagedPiRuntimeError(
            f"managed Pi retention {label} generation is invalid"
        )
    version = _safe_version(
        value.get("version"),
        label=f"retention {label} generation version",
    )
    manifest_digest = _sha256_text(
        value.get("manifestSha256"),
        label=f"retention {label} manifest digest",
    )
    runtime_dir = str(value.get("runtimeDir") or "")
    if not runtime_dir:
        raise ManagedPiRuntimeError(
            f"managed Pi retention {label} runtime directory is invalid"
        )
    return version, manifest_digest, runtime_dir


def _verify_retention_candidate(
    runtime_root: Path,
    action: Mapping[str, object],
) -> None:
    version = _safe_version(
        action.get("version"),
        label="retention candidate version",
    )
    target = runtime_root / version
    if str(action.get("target") or "") != str(target):
        raise ManagedPiRuntimeError(
            "managed Pi retention candidate path changed after dry-run"
        )
    try:
        directory_stat = target.stat(follow_symlinks=False)
    except OSError as exc:
        raise ManagedPiRuntimeError(
            "managed Pi retention candidate disappeared after dry-run"
        ) from exc
    if target.is_symlink() or not stat.S_ISDIR(directory_stat.st_mode):
        raise ManagedPiRuntimeError(
            "managed Pi retention candidate is no longer a real directory"
        )
    expected_device = action.get("directoryDevice")
    expected_inode = action.get("directoryInode")
    if (
        expected_device != directory_stat.st_dev
        or expected_inode != directory_stat.st_ino
    ):
        raise ManagedPiRuntimeError(
            "managed Pi retention candidate identity changed after dry-run"
        )
    verified = _verified_generation(runtime_root, target)
    if (
        verified.runtime_version != version
        or verified.manifest_sha256 != str(action.get("manifestSha256") or "")
    ):
        raise ManagedPiRuntimeError(
            "managed Pi retention candidate contents changed after dry-run"
        )


def _quarantine_retention_candidate(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise ManagedPiRuntimeError(
            "managed Pi retention quarantine destination already exists"
        )
    os.replace(source, destination)


def _verify_quarantined_retention_candidate(
    runtime_root: Path,
    batch_root: Path,
    quarantined: Path,
    action: Mapping[str, object],
) -> None:
    version = _safe_version(
        action.get("version"),
        label="quarantined runtime version",
    )
    expected = batch_root / version
    if quarantined != expected or not _is_within(
        quarantined.resolve(strict=False),
        batch_root.resolve(strict=True),
    ):
        raise ManagedPiRuntimeError(
            "managed Pi quarantine cleanup target escaped its batch"
        )
    quarantined_stat = quarantined.stat(follow_symlinks=False)
    if (
        quarantined.is_symlink()
        or not stat.S_ISDIR(quarantined_stat.st_mode)
        or action.get("directoryDevice") != quarantined_stat.st_dev
        or action.get("directoryInode") != quarantined_stat.st_ino
    ):
        raise ManagedPiRuntimeError(
            "managed Pi quarantined generation identity changed"
        )
    verified = _verified_generation(runtime_root, quarantined)
    if (
        verified.runtime_version != version
        or verified.manifest_sha256
        != str(action.get("manifestSha256") or "")
    ):
        raise ManagedPiRuntimeError(
            "managed Pi quarantined generation contents changed"
        )


def _rollback_quarantined_generations(
    moved: list[tuple[Path, Path, Mapping[str, object]]],
) -> dict[str, str]:
    failures: dict[str, str] = {}
    for source, quarantined, action in reversed(moved):
        version = str(action.get("version") or "")
        try:
            if source.exists() or source.is_symlink():
                raise ManagedPiRuntimeError(
                    "managed Pi rollback destination unexpectedly exists"
                )
            os.replace(quarantined, source)
        except (OSError, ManagedPiRuntimeError) as exc:
            failures[version] = str(exc)
    return failures


def _retention_failure_actions(
    raw_actions: object,
    *,
    moved: list[tuple[Path, Path, Mapping[str, object]]],
    failed_version: str = "",
    rollback_failures: Mapping[str, str],
) -> list[dict[str, object]]:
    moved_versions = {
        str(action.get("version") or "") for _source, _target, action in moved
    }
    result: list[dict[str, object]] = []
    if not isinstance(raw_actions, list):
        return result
    for raw_action in raw_actions:
        action = dict(raw_action) if isinstance(raw_action, Mapping) else {}
        if action.get("status") != "planned":
            result.append(action)
            continue
        version = str(action.get("version") or "")
        if version in rollback_failures:
            action["status"] = "rollback_failed"
            action["failure"] = rollback_failures[version]
        elif version in moved_versions:
            action["status"] = "rolled_back"
        elif version == failed_version:
            action["status"] = "failed"
        else:
            action["status"] = "not_attempted"
        result.append(action)
    return result


def _remove_empty_quarantine(batch_root: Path, quarantine_root: Path) -> None:
    for path in (batch_root, quarantine_root):
        try:
            path.rmdir()
        except OSError:
            pass


def _canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _native_skill_body(content: str) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---"):
        return normalized.strip()
    end_index = normalized.find("\n---", 3)
    return (
        normalized if end_index < 0 else normalized[end_index + 4 :]
    ).strip()


def build_managed_pi_runtime_manifest(
    payload_dir: str | Path,
    *,
    runtime_version: str,
    pi_version: str,
    launch_kind: str,
    pi_entrypoint: str,
    extension_entrypoint: str,
    node_entrypoint: str = "",
    tools: tuple[str, ...] = CONTROL_TOOL_IDS,
    source_repository: str,
    source_commit: str,
    source_package: str,
    protocol_version: str = "2",
    runtime_methods: tuple[str, ...] = (),
    source_contract_sha256: str = "",
    handlers_commit: str = "",
) -> dict[str, object]:
    root = Path(payload_dir).expanduser().resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise ManagedPiRuntimeError("managed Pi payload must be a real directory")
    version = _safe_version(runtime_version, label="runtime version")
    normalized_tools = _validate_tools(tools)
    if launch_kind not in {"node", "standalone"}:
        raise ManagedPiRuntimeError("managed Pi launch kind must be node or standalone")
    critical = [
        _safe_relative_path(pi_entrypoint),
        _safe_relative_path(extension_entrypoint),
    ]
    if launch_kind == "node":
        if not node_entrypoint:
            raise ManagedPiRuntimeError("node launch requires nodeEntrypoint")
        critical.append(_safe_relative_path(node_entrypoint))
    elif node_entrypoint:
        raise ManagedPiRuntimeError("standalone launch must not define nodeEntrypoint")

    files: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.name == MANIFEST_NAME and path.parent == root:
            continue
        if path.is_symlink():
            raise ManagedPiRuntimeError(f"managed Pi payload contains a symlink: {path.relative_to(root)}")
        if not path.is_file():
            continue
        relative = PurePosixPath(path.relative_to(root).as_posix())
        files.append(
            {
                "path": relative.as_posix(),
                "sha256": _sha256_file(path),
                "byteSize": path.stat().st_size,
                "executable": bool(path.stat().st_mode & stat.S_IXUSR),
            }
        )
    file_paths = {str(item["path"]) for item in files}
    missing = [path.as_posix() for path in critical if path.as_posix() not in file_paths]
    if missing:
        raise ManagedPiRuntimeError(f"managed Pi payload is missing critical files: {', '.join(missing)}")
    normalized_protocol = _require_supported_protocol(protocol_version)
    normalized_runtime_methods = _runtime_methods(runtime_methods, normalized_protocol)
    source: dict[str, object] = {
        "repository": str(source_repository).strip(),
        "commit": str(source_commit).strip(),
        "package": str(source_package).strip(),
    }
    if normalized_protocol == "2":
        source["sourceContractSha256"] = _sha256_text(
            source_contract_sha256, label="runtime source contract digest"
        )
        normalized_handlers_commit = str(handlers_commit or "").strip().lower()
        if not _GIT_COMMIT_PATTERN.fullmatch(normalized_handlers_commit):
            raise ManagedPiRuntimeError("managed Pi Room handlers commit is invalid")
        source["handlersCommit"] = normalized_handlers_commit
    manifest: dict[str, object] = {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "runtimeVersion": version,
        "piVersion": str(pi_version).strip(),
        "runtimeProtocolVersion": normalized_protocol,
        "runtimeMethods": list(normalized_runtime_methods),
        "platform": _current_platform(),
        "architecture": _current_architecture(),
        "launchKind": launch_kind,
        "piEntrypoint": critical[0].as_posix(),
        "nodeEntrypoint": _safe_relative_path(node_entrypoint).as_posix() if node_entrypoint else "",
        "extensionEntrypoint": critical[1].as_posix(),
        "tools": list(normalized_tools),
        "createdAtMs": int(time.time() * 1000),
        "source": source,
        "files": files,
    }
    _validate_manifest(manifest)
    return manifest


def write_managed_pi_runtime_manifest(path: str | Path, manifest: Mapping[str, object]) -> None:
    payload = dict(manifest)
    _validate_manifest(payload)
    _atomic_write_bytes(
        Path(path),
        (json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        mode=0o644,
    )


def _load_installation(
    *,
    runtime_root: Path,
    runtime_dir: Path,
    expected_manifest_sha256: str,
    expected_pi_version: str,
    verify_all_files: bool,
    expected_runtime_version: str = "",
) -> ManagedPiRuntimeInstallation:
    root_resolved = runtime_root.expanduser().resolve(strict=False)
    if runtime_dir.is_symlink():
        raise ManagedPiRuntimeError("managed Pi runtime version directory must not be a symlink")
    try:
        runtime_resolved = runtime_dir.expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise ManagedPiRuntimeError("managed Pi runtime version is missing") from exc
    if not runtime_resolved.is_dir() or not _is_within(runtime_resolved, root_resolved):
        raise ManagedPiRuntimeError("managed Pi runtime version escapes the runtime root")
    manifest_path = runtime_resolved / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ManagedPiRuntimeError("managed Pi runtime manifest is missing")
    manifest_bytes = _read_limited_bytes(manifest_path)
    actual_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_manifest_sha256 != expected_manifest_sha256:
        raise ManagedPiRuntimeError("managed Pi runtime manifest digest does not match the active pointer")
    manifest = _parse_json_object(manifest_bytes, manifest_path)
    _validate_manifest(manifest)
    runtime_version = _safe_version(manifest.get("runtimeVersion"), label="runtime version")
    directory_version = expected_runtime_version or runtime_resolved.name
    if runtime_version != directory_version:
        raise ManagedPiRuntimeError("managed Pi runtime version does not match its directory")
    pi_version = str(manifest.get("piVersion") or "").strip()
    if expected_pi_version and pi_version != expected_pi_version:
        raise ManagedPiRuntimeError(
            f"managed Pi version {pi_version or '<empty>'} does not match required {expected_pi_version}"
        )
    if _normalize_platform(manifest.get("platform")) != _current_platform():
        raise ManagedPiRuntimeError("managed Pi runtime platform does not match this machine")
    if _normalize_architecture(manifest.get("architecture")) != _current_architecture():
        raise ManagedPiRuntimeError("managed Pi runtime architecture does not match this machine")

    launch_kind = str(manifest.get("launchKind") or "")
    pi_relative = _safe_relative_path(manifest.get("piEntrypoint"))
    extension_relative = _safe_relative_path(manifest.get("extensionEntrypoint"))
    node_value = str(manifest.get("nodeEntrypoint") or "").strip()
    node_relative = _safe_relative_path(node_value) if node_value else None
    if launch_kind == "node" and node_relative is None:
        raise ManagedPiRuntimeError("managed Pi node runtime is missing nodeEntrypoint")
    if launch_kind == "standalone" and node_relative is not None:
        raise ManagedPiRuntimeError("managed Pi standalone runtime must not define nodeEntrypoint")

    items = _manifest_file_items(manifest)
    by_path: dict[str, Mapping[str, object]] = {}
    for item in items:
        relative = _safe_relative_path(item.get("path"))
        key = relative.as_posix()
        if key in by_path:
            raise ManagedPiRuntimeError(f"managed Pi manifest contains duplicate file: {key}")
        by_path[key] = item
    critical = [pi_relative, extension_relative, *([node_relative] if node_relative is not None else [])]
    for relative in critical:
        if relative.as_posix() not in by_path:
            raise ManagedPiRuntimeError(f"managed Pi manifest omits critical file: {relative.as_posix()}")

    verified_paths = set(by_path) if verify_all_files else {path.as_posix() for path in critical}
    for relative_text in sorted(verified_paths):
        _verify_manifest_file(runtime_resolved, _safe_relative_path(relative_text), by_path[relative_text])
    if verify_all_files:
        actual_paths = _runtime_file_paths(runtime_resolved)
        expected_paths = set(by_path) | {MANIFEST_NAME}
        unexpected = sorted(actual_paths - expected_paths)
        missing = sorted(expected_paths - actual_paths)
        if unexpected or missing:
            detail = unexpected[0] if unexpected else missing[0]
            kind = "unexpected" if unexpected else "missing"
            raise ManagedPiRuntimeError(f"managed Pi runtime has {kind} file: {detail}")

    executable = _resolve_runtime_file(runtime_resolved, pi_relative)
    extension = _resolve_runtime_file(runtime_resolved, extension_relative)
    node_executable = ""
    if node_relative is not None:
        node_path = _resolve_runtime_file(runtime_resolved, node_relative)
        if not _is_executable(node_path):
            raise ManagedPiRuntimeError("managed Pi Node runtime is not executable")
        node_executable = str(node_path)
    elif not _is_executable(executable):
        raise ManagedPiRuntimeError("managed Pi standalone entrypoint is not executable")
    return ManagedPiRuntimeInstallation(
        runtime_root=root_resolved,
        runtime_dir=runtime_resolved,
        runtime_version=runtime_version,
        pi_version=pi_version,
        executable=executable,
        node_executable=node_executable,
        extension_path=extension,
        tools=_validate_tools(manifest.get("tools")),
        manifest_sha256=actual_manifest_sha256,
        # Missing protocol metadata identifies a historical v1 generation; execution
        # entrypoints reject it while inspection/retention can still read it.
        protocol_version=_protocol_version(manifest.get("runtimeProtocolVersion") or "1"),
        runtime_methods=_runtime_methods(
            manifest.get("runtimeMethods"),
            _protocol_version(manifest.get("runtimeProtocolVersion") or "1"),
        ),
    )


def _validate_manifest(manifest: Mapping[str, object]) -> None:
    try:
        validate_contract(dict(manifest), _MANIFEST_CONTRACT)
    except ContractValidationError as exc:
        raise ManagedPiRuntimeError(f"managed Pi runtime manifest is invalid: {exc}") from exc
    _safe_version(manifest.get("runtimeVersion"), label="runtime version")
    if not str(manifest.get("piVersion") or "").strip():
        raise ManagedPiRuntimeError("managed Pi manifest piVersion is empty")
    _validate_tools(manifest.get("tools"))
    protocol_version = _protocol_version(manifest.get("runtimeProtocolVersion") or "1")
    _runtime_methods(manifest.get("runtimeMethods"), protocol_version)
    source = manifest.get("source")
    if not isinstance(source, Mapping) or any(
        not str(source.get(key) or "").strip() for key in ("repository", "commit", "package")
    ):
        raise ManagedPiRuntimeError("managed Pi manifest source provenance is incomplete")
    if protocol_version == "2":
        _sha256_text(
            source.get("sourceContractSha256"), label="runtime source contract digest"
        )
        handlers_commit = str(source.get("handlersCommit") or "").strip().lower()
        if not _GIT_COMMIT_PATTERN.fullmatch(handlers_commit):
            raise ManagedPiRuntimeError("managed Pi Room handlers commit is invalid")
    items = _manifest_file_items(manifest)
    if not items:
        raise ManagedPiRuntimeError("managed Pi manifest does not list files")
    for item in items:
        _safe_relative_path(item.get("path"))
        _sha256_text(item.get("sha256"), label="runtime file digest")
        size = item.get("byteSize")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ManagedPiRuntimeError("managed Pi manifest file size is invalid")
        if not isinstance(item.get("executable"), bool):
            raise ManagedPiRuntimeError("managed Pi manifest executable flag is invalid")


def _require_supported_protocol(value: object) -> str:
    version = _protocol_version(value)
    if version != "2":
        raise ManagedPiRuntimeError(
            "managed Pi protocol 1 is retired; rebuild and install a protocol 2 Host"
        )
    return version


def _protocol_version(value: object) -> str:
    normalized = str(value or "").strip()
    if normalized not in {"1", "2"}:
        raise ManagedPiRuntimeError("managed Pi runtimeProtocolVersion must be 1 or 2")
    return normalized


def _runtime_methods(value: object, protocol_version: str) -> tuple[str, ...]:
    if value is None and protocol_version == "1":
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > _MAX_RUNTIME_METHODS:
        raise ManagedPiRuntimeError("managed Pi runtime method list is invalid")
    methods = tuple(str(item or "").strip() for item in value)
    if len(set(methods)) != len(methods) or any(
        not _RUNTIME_METHOD_PATTERN.fullmatch(method) for method in methods
    ):
        raise ManagedPiRuntimeError("managed Pi runtime method list is invalid")
    if protocol_version == "2" and not set(REQUIRED_SESSION_RUNTIME_METHODS).issubset(methods):
        raise ManagedPiRuntimeError(
            "managed Pi protocol v2 omits required Session runtime methods"
        )
    return methods


def _manifest_file_items(manifest: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = manifest.get("files")
    if not isinstance(raw, list) or len(raw) > _MAX_RUNTIME_FILES:
        raise ManagedPiRuntimeError("managed Pi manifest file list is invalid")
    if any(not isinstance(item, Mapping) for item in raw):
        raise ManagedPiRuntimeError("managed Pi manifest file entry is invalid")
    return [item for item in raw if isinstance(item, Mapping)]


def _verify_manifest_file(runtime_dir: Path, relative: PurePosixPath, item: Mapping[str, object]) -> None:
    path = _resolve_runtime_file(runtime_dir, relative)
    expected_size = int(item.get("byteSize") or 0)
    if path.stat().st_size != expected_size:
        raise ManagedPiRuntimeError(f"managed Pi runtime file size mismatch: {relative.as_posix()}")
    if _sha256_file(path) != str(item.get("sha256") or ""):
        raise ManagedPiRuntimeError(f"managed Pi runtime file digest mismatch: {relative.as_posix()}")
    if bool(item.get("executable")) and not _is_executable(path):
        raise ManagedPiRuntimeError(f"managed Pi runtime file is not executable: {relative.as_posix()}")


def _resolve_payload_file(root: Path, relative: PurePosixPath) -> Path:
    return _resolve_runtime_file(root, relative)


def _resolve_runtime_file(root: Path, relative: PurePosixPath) -> Path:
    candidate = root.joinpath(*relative.parts)
    if candidate.is_symlink() or _has_symlink_component(candidate, root):
        raise ManagedPiRuntimeError(f"managed Pi runtime contains a symlink: {relative.as_posix()}")
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ManagedPiRuntimeError(f"managed Pi runtime file is missing: {relative.as_posix()}") from exc
    root_resolved = root.resolve(strict=True)
    if not resolved.is_file() or not _is_within(resolved, root_resolved):
        raise ManagedPiRuntimeError(f"managed Pi runtime file escapes its package: {relative.as_posix()}")
    return resolved


def _safe_relative_path(value: object) -> PurePosixPath:
    text = str(value or "").strip()
    path = PurePosixPath(text)
    if (
        not text
        or "\\" in text
        or path.is_absolute()
        or path.as_posix() != text
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ManagedPiRuntimeError(f"unsafe managed Pi runtime path: {text or '<empty>'}")
    return path


def _safe_version(value: object, *, label: str) -> str:
    version = str(value or "").strip()
    if not _VERSION_PATTERN.fullmatch(version):
        raise ManagedPiRuntimeError(f"{label} is invalid")
    return version


def _sha256_text(value: object, *, label: str) -> str:
    digest = str(value or "").strip().lower()
    if not _SHA256_PATTERN.fullmatch(digest):
        raise ManagedPiRuntimeError(f"{label} is invalid")
    return digest


def _validate_tools(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 128:
        raise ManagedPiRuntimeError("managed Pi tool list is invalid")
    tools = tuple(str(item or "").strip() for item in value)
    if len(set(tools)) != len(tools) or any(not _TOOL_PATTERN.fullmatch(tool) for tool in tools):
        raise ManagedPiRuntimeError("managed Pi tool list is invalid")
    return tools


@contextmanager
def _managed_runtime_lock(
    app_support: str | Path,
    *,
    create_runtime_root: bool = True,
) -> Iterator[Path]:
    """Serialize every pointer, generation, acceptance, and retention mutation.

    Lock ordering is intentionally one-level: callers acquire this file lock
    before reading the pointer or enumerating ``PiRuntime`` and never acquire a
    second managed-runtime lock while it is held.
    """

    app_root_input = Path(app_support).expanduser()
    if app_root_input.exists() and app_root_input.is_symlink():
        raise ManagedPiRuntimeError(
            "managed Pi application support root must not be a symlink"
        )
    app_root_input.mkdir(parents=True, exist_ok=True, mode=0o700)
    app_root = app_root_input.resolve(strict=True)
    lock_path = app_root / LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ManagedPiRuntimeError(
            "cannot open managed Pi runtime lifecycle lock"
        ) from exc
    try:
        os.fchmod(descriptor, 0o600)
        descriptor_stat = os.fstat(descriptor)
        path_stat = lock_path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(descriptor_stat.st_mode)
            or descriptor_stat.st_nlink != 1
            or descriptor_stat.st_dev != path_stat.st_dev
            or descriptor_stat.st_ino != path_stat.st_ino
            or lock_path.is_symlink()
            or stat.S_IMODE(descriptor_stat.st_mode) != 0o600
        ):
            raise ManagedPiRuntimeError(
                "managed Pi runtime lifecycle lock is not a 0600 regular file"
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        runtime_root = _runtime_root(app_root)
        if create_runtime_root:
            runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if runtime_root.exists() and (
            runtime_root.is_symlink() or not runtime_root.is_dir()
        ):
            raise ManagedPiRuntimeError(
                "managed Pi runtime root must be a real directory"
            )
        yield runtime_root
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _runtime_root(app_support: str | Path) -> Path:
    root = Path(app_support).expanduser() / "PiRuntime"
    if root.exists() and root.is_symlink():
        raise ManagedPiRuntimeError("managed Pi runtime root must not be a symlink")
    return root.resolve(strict=False)


def _runtime_file_paths(root: Path) -> set[str]:
    paths: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ManagedPiRuntimeError(f"managed Pi runtime contains a symlink: {path.relative_to(root)}")
        if path.is_file():
            paths.add(path.relative_to(root).as_posix())
    return paths


def _has_symlink_component(path: Path, root: Path) -> bool:
    current = path
    root_resolved = root.resolve(strict=True)
    while current != root_resolved:
        if current.is_symlink():
            return True
        parent = current.parent
        if parent == current:
            return True
        current = parent
    return False


def _read_json_object(path: Path) -> dict[str, object]:
    return _parse_json_object(_read_limited_bytes(path), path)


def _read_limited_bytes(path: Path) -> bytes:
    try:
        size = path.stat().st_size
        if size > _MAX_JSON_BYTES:
            raise ManagedPiRuntimeError(f"managed Pi JSON file is too large: {path.name}")
        return path.read_bytes()
    except OSError as exc:
        raise ManagedPiRuntimeError(f"cannot read managed Pi JSON file: {path.name}") from exc


def _parse_json_object(data: bytes, path: Path) -> dict[str, object]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagedPiRuntimeError(f"managed Pi JSON file is invalid: {path.name}") from exc
    if not isinstance(value, dict):
        raise ManagedPiRuntimeError(f"managed Pi JSON file must contain an object: {path.name}")
    return value


def _atomic_write_bytes(path: Path, data: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_executable(path: Path) -> bool:
    return bool(path.stat().st_mode & stat.S_IXUSR)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _current_platform() -> str:
    return _normalize_platform(platform.system())


def _normalize_platform(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return {"macos": "darwin", "osx": "darwin"}.get(normalized, normalized)


def _current_architecture() -> str:
    return _normalize_architecture(platform.machine())


def _normalize_architecture(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return {"aarch64": "arm64", "amd64": "x86_64"}.get(normalized, normalized)
