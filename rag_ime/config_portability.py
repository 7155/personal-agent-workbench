from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

import yaml

from .db import apply_database_migrations, sqlite_connection
from .keychain_secrets import (
    MODEL_INSTANT_ACCOUNT,
    MODEL_KEYCHAIN_SERVICE,
    MODEL_KNOWLEDGE_ACCOUNT,
    write_keychain_secret,
)
from .settings_schema import default_settings, flatten_settings, settings_schema, unflatten_settings
from .settings_store import ManagementSettingsStore
from .text_utils import compact_whitespace, now_ms


USER_CONFIG_SCHEMA_VERSION = "rag-ime.user-config.v1"
BACKUP_SCHEMA_VERSION = "rag-ime.portable-backup.v1"
CONFIG_PREVIEW_SCHEMA_VERSION = "rag-ime.user-config-preview.v1"
_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_CONFIG_FILE_BYTES = 1024 * 1024
_MAX_RIME_FILE_BYTES = 2 * 1024 * 1024
_PROVIDER_KEYS = {"provider", "endpoint", "model", "apiKey", "headers"}


def preview_user_configuration(
    payload: Mapping[str, object],
    *,
    settings_store: ManagementSettingsStore,
) -> dict[str, object]:
    config, source = _configuration_payload_with_source(payload)
    errors: list[str] = []
    warnings: list[str] = []
    schema_version = compact_whitespace(str(config.get("schemaVersion") or ""))
    if schema_version != USER_CONFIG_SCHEMA_VERSION:
        errors.append(f"schemaVersion must be {USER_CONFIG_SCHEMA_VERSION}")
    unknown_top_level = sorted(set(config) - {"schemaVersion", "settings", "providers"})
    errors.extend(f"unknown configuration field: {key}" for key in unknown_top_level)

    settings_value = config.get("settings")
    if settings_value is not None and not isinstance(settings_value, Mapping):
        errors.append("settings must be an object")
    settings = dict(settings_value) if isinstance(settings_value, Mapping) else {}
    normalized_settings, setting_errors, unknown_settings = _validated_settings(settings)
    errors.extend(setting_errors)
    if unknown_settings:
        errors.extend(f"unknown setting: {key}" for key in unknown_settings)

    providers_value = config.get("providers")
    if providers_value is not None and not isinstance(providers_value, Mapping):
        errors.append("providers must be an object")
    providers = dict(providers_value) if isinstance(providers_value, Mapping) else {}
    provider_preview: dict[str, object] = {}
    for slot_name in ("instant", "knowledge", "voice"):
        raw = providers.get(slot_name)
        if raw is None:
            continue
        if not isinstance(raw, Mapping):
            errors.append(f"providers.{slot_name} must be an object")
            continue
        slot = dict(raw)
        slot_errors = _provider_errors(slot_name, slot)
        errors.extend(slot_errors)
        provider_preview[slot_name] = {
            "provider": compact_whitespace(str(slot.get("provider") or "")),
            "endpoint": compact_whitespace(str(slot.get("endpoint") or "")),
            "model": compact_whitespace(str(slot.get("model") or "")),
            "secretProvided": bool(
                compact_whitespace(str(slot.get("apiKey") or slot.get("accessToken") or ""))
            ),
            "secretWillBePreserved": not bool(
                compact_whitespace(str(slot.get("apiKey") or slot.get("accessToken") or ""))
            ),
        }
    unknown_provider_slots = sorted(set(providers) - {"instant", "knowledge", "voice"})
    errors.extend(f"unknown provider slot: {name}" for name in unknown_provider_slots)
    flat_settings = flatten_settings(normalized_settings)
    requires_remote_confirmation = any(
        flat_settings.get(key) is True
        for key in ("activeRag.allowRemoteModel", "privacy.allowRemoteModelForActiveRag")
    )
    if not normalized_settings:
        warnings.append("配置没有覆盖管理设置；现有设置会保持不变")
    return {
        "schemaVersion": CONFIG_PREVIEW_SCHEMA_VERSION,
        "ok": not errors,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "settings": normalized_settings,
        "settingCount": len(flat_settings),
        "providers": provider_preview,
        "requiresRemoteModelConfirmation": requires_remote_confirmation,
        "configurationHash": _configuration_hash(config),
        "requiresConfirmation": "IMPORT RAG-IME CONFIGURATION",
        "secretsEchoed": False,
        "source": source,
    }


def apply_user_configuration(
    payload: Mapping[str, object],
    *,
    settings_store: ManagementSettingsStore,
    support_directory: str | Path | None = None,
) -> dict[str, object]:
    preview = preview_user_configuration(payload, settings_store=settings_store)
    if not bool(preview["valid"]):
        raise ValueError("; ".join(str(item) for item in preview["errors"]))
    config, source = _configuration_payload_with_source(payload)
    confirm_remote = compact_whitespace(str(payload.get("confirmRemoteModel") or ""))
    settings = preview["settings"] if isinstance(preview.get("settings"), Mapping) else {}
    changed_keys: tuple[str, ...] = ()
    audit_id = 0
    if settings:
        result = settings_store.update_settings(
            settings,
            updated_by="configuration-import",
            confirm_text=confirm_remote,
        )
        changed_keys = result.changed_keys
        audit_id = int(result.audit_id)

    support = (
        Path(support_directory).expanduser()
        if support_directory is not None
        else Path.home() / "Library" / "Application Support" / "RagIme"
    )
    providers_value = config.get("providers")
    providers = dict(providers_value) if isinstance(providers_value, Mapping) else {}
    provider_results: dict[str, object] = {}
    for slot_name in ("instant", "knowledge", "voice"):
        raw = providers.get(slot_name)
        if not isinstance(raw, Mapping):
            continue
        if slot_name == "voice":
            provider_results[slot_name] = _apply_voice_provider(dict(raw), support_directory=support)
        else:
            provider_results[slot_name] = _apply_model_provider(
                slot_name,
                dict(raw),
                support_directory=support,
            )
    return {
        "schemaVersion": "rag-ime.user-config-apply.v1",
        "ok": True,
        "changedKeys": list(changed_keys),
        "auditId": audit_id,
        "providers": provider_results,
        "secretsEchoed": False,
        "requiresRestart": bool(provider_results),
        "source": source,
    }


def export_portable_backup(
    *,
    db_path: str | Path,
    settings_store: ManagementSettingsStore,
    destination: str | Path,
    rime_user_dir: str | Path | None = None,
    support_directory: str | Path | None = None,
) -> dict[str, object]:
    source_db = Path(db_path).expanduser()
    if not source_db.exists():
        raise ValueError(f"database not found: {source_db}")
    target = Path(destination).expanduser()
    if target.suffix.lower() not in {".zip", ".ragime-backup"}:
        target = target.with_suffix(".ragime-backup")
    target.parent.mkdir(parents=True, exist_ok=True)
    support = (
        Path(support_directory).expanduser()
        if support_directory is not None
        else Path.home() / "Library" / "Application Support" / "RagIme"
    )
    rime_dir = Path(rime_user_dir).expanduser() if rime_user_dir is not None else Path.home() / "Library" / "Rime"

    with tempfile.TemporaryDirectory(prefix="rag-ime-backup-", dir=target.parent) as temporary:
        staging = Path(temporary)
        database_snapshot = staging / "database" / "rag-ime.sqlite"
        database_snapshot.parent.mkdir(parents=True, exist_ok=True)
        _backup_sqlite(source_db, database_snapshot)
        settings_path = staging / "configuration" / "settings.json"
        _write_json(settings_path, settings_store.get_settings(include_sensitive=False))
        provider_path = staging / "configuration" / "provider-metadata.json"
        _write_json(provider_path, _provider_metadata(support))

        copied_rime_files: list[str] = []
        safe_rime_files, skipped_sensitive_rime_files = _rime_configuration_selection(rime_dir)
        for source in safe_rime_files:
            relative = source.relative_to(rime_dir)
            destination_path = staging / "rime" / relative
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination_path)
            copied_rime_files.append(relative.as_posix())

        from .knowledge_library.vault_backup import export_records
        vault_records = export_records((support / 'Knowledge' / 'knowledge.sqlite').resolve())
        if vault_records is not None:
            _write_json(staging / 'knowledge' / 'vault-control.json', vault_records)
        counts = _database_counts(database_snapshot)
        _remove_sqlite_sidecars(database_snapshot)
        entries = _manifest_entries(staging)
        manifest = {
            "schemaVersion": BACKUP_SCHEMA_VERSION,
            "createdAtMs": now_ms(),
            "database": {"path": "database/rag-ime.sqlite", "counts": counts},
            "settingsIncluded": True,
            "rimeFiles": copied_rime_files,
            "rimeFilesSkippedAsSensitive": skipped_sensitive_rime_files,
            "entries": entries,
            "exclusions": [
                "API keys and access tokens",
                "model weights",
                "runtime caches",
                "logs and traces",
                "Rime userdb binary directories",
            ],
        }
        _write_json(staging / "manifest.json", manifest)
        temporary_target = target.with_name(f".{target.name}.tmp")
        if temporary_target.exists():
            temporary_target.unlink()
        with zipfile.ZipFile(temporary_target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for file_path in sorted(path for path in staging.rglob("*") if path.is_file()):
                archive.write(file_path, file_path.relative_to(staging).as_posix())
        os.chmod(temporary_target, 0o600)
        temporary_target.replace(target)
    return {
        "schemaVersion": "rag-ime.portable-backup-export.v1",
        "ok": True,
        "path": str(target),
        "sizeBytes": target.stat().st_size,
        "databaseCounts": counts,
        "rimeFileCount": len(copied_rime_files),
        "rimeFilesSkippedAsSensitive": len(skipped_sensitive_rime_files),
        "secretsIncluded": False,
    }


def preview_portable_restore(*, archive_path: str | Path) -> dict[str, object]:
    source = Path(archive_path).expanduser()
    if not source.exists():
        raise ValueError(f"backup not found: {source}")
    archive_hash = _sha256_file(source)
    with zipfile.ZipFile(source, "r") as archive:
        _validate_archive_members(archive)
        manifest = _read_archive_json(archive, "manifest.json")
        if manifest.get("schemaVersion") != BACKUP_SCHEMA_VERSION:
            raise ValueError("unsupported backup schemaVersion")
        entries = manifest.get("entries") if isinstance(manifest.get("entries"), Mapping) else {}
        archive_files = {
            info.filename
            for info in archive.infolist()
            if not info.is_dir()
        }
        if set(entries) != archive_files - {"manifest.json"}:
            raise ValueError("backup manifest does not cover every archive entry")
        for name, expected in entries.items():
            if not isinstance(name, str) or not isinstance(expected, str):
                raise ValueError("backup manifest contains an invalid entry")
            try:
                payload = archive.read(name)
            except KeyError as exc:
                raise ValueError(f"backup entry is missing: {name}") from exc
            if hashlib.sha256(payload).hexdigest() != expected:
                raise ValueError(f"backup checksum mismatch: {name}")
        db_name = str(dict(manifest.get("database") or {}).get("path") or "")
        if db_name != "database/rag-ime.sqlite":
            raise ValueError("backup database path is invalid")
        with tempfile.TemporaryDirectory(prefix="rag-ime-restore-preview-") as temporary:
            db_path = Path(temporary) / "rag-ime.sqlite"
            db_path.write_bytes(archive.read(db_name))
            counts = _database_counts(db_path)
            migration_version = _database_migration_version(db_path)
    return {
        "schemaVersion": "rag-ime.portable-restore-preview.v1",
        "ok": True,
        "valid": True,
        "path": str(source),
        "restoreToken": archive_hash,
        "createdAtMs": int(manifest.get("createdAtMs") or 0),
        "databaseCounts": counts,
        "databaseMigrationVersion": migration_version,
        "rimeFileCount": len(manifest.get("rimeFiles") or []),
        "exclusions": list(manifest.get("exclusions") or []),
        "requiresConfirmation": "RESTORE RAG-IME",
        "requiresRestart": True,
    }


def restore_portable_backup(
    *,
    archive_path: str | Path,
    db_path: str | Path,
    settings_store: ManagementSettingsStore,
    restore_token: str,
    confirm_text: str,
    rime_user_dir: str | Path | None = None,
    support_directory: str | Path | None = None,
    post_restore: Callable[[Path], None] | None = None,
) -> dict[str, object]:
    if confirm_text != "RESTORE RAG-IME":
        raise ValueError("restore requires confirmText=RESTORE RAG-IME")
    preview = preview_portable_restore(archive_path=archive_path)
    if restore_token != str(preview["restoreToken"]):
        raise ValueError("backup changed after preview; preview it again")
    source = Path(archive_path).expanduser()
    target_db = Path(db_path).expanduser()
    rollback_path = source.with_name(f"rag-ime-rollback-{now_ms()}.ragime-backup")
    export_portable_backup(
        db_path=target_db,
        settings_store=settings_store,
        destination=rollback_path,
        rime_user_dir=rime_user_dir,
        support_directory=support_directory,
    )
    rime_dir = Path(rime_user_dir).expanduser() if rime_user_dir is not None else Path.home() / "Library" / "Rime"
    support = (
        Path(support_directory).expanduser()
        if support_directory is not None
        else Path.home() / "Library" / "Application Support" / "RagIme"
    )
    with tempfile.TemporaryDirectory(prefix="rag-ime-restore-") as temporary:
        staging = Path(temporary)
        with zipfile.ZipFile(source, "r") as archive:
            _validate_archive_members(archive)
            archive.extractall(staging)
        restored_db = staging / "database" / "rag-ime.sqlite"
        from .knowledge_library.vault_backup import export_records, restore_records
        knowledge_database = (support / 'Knowledge' / 'knowledge.sqlite').resolve()
        vault_before = export_records(knowledge_database)
        vault_backup_path = staging / 'knowledge' / 'vault-control.json'
        vault_packet = json.loads(vault_backup_path.read_text()) if vault_backup_path.exists() else None
        current_snapshot = staging / "current.sqlite"
        _backup_sqlite(target_db, current_snapshot)
        rime_sources = (
            [item for item in sorted((staging / "rime").rglob("*")) if item.is_file()]
            if (staging / "rime").exists()
            else []
        )
        rime_backups: dict[Path, Path | None] = {}
        for source_file in rime_sources:
            relative = source_file.relative_to(staging / "rime")
            destination = rime_dir / relative
            if destination.is_symlink():
                raise ValueError(f"refusing to restore through a Rime symlink: {relative.as_posix()}")
            backup = staging / "rollback" / "rime" / relative
            rime_backups[destination] = _snapshot_optional_file(destination, backup)
        provider_paths = [
            support / "predictor.env",
            support / "deepseek.env",
            support / "voice-provider.json",
        ]
        provider_backups = {
            path: _snapshot_optional_file(path, staging / "rollback" / "support" / path.name)
            for path in provider_paths
        }
        provider_metadata = _read_json_file(staging / "configuration" / "provider-metadata.json")
        restored_provider_slots: list[str] = []
        try:
            _restore_sqlite(restored_db, target_db)
            with sqlite_connection(target_db) as conn:
                apply_database_migrations(conn)
            for source_file in rime_sources:
                relative = source_file.relative_to(staging / "rime")
                destination = rime_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary_file = destination.with_name(f".{destination.name}.restore-tmp")
                shutil.copy2(source_file, temporary_file)
                temporary_file.replace(destination)
            restored_provider_slots = _restore_provider_metadata(
                provider_metadata,
                support_directory=support,
            )
            if vault_packet is not None:
                restore_records(knowledge_database, vault_packet)
                # Restored note links never resurrect a prior adoption as an
                # active constraint without rechecking current Markdown.
                from .memory_actions import mutate_memory_action
                with sqlite_connection(target_db) as conn:
                    conn.row_factory = sqlite3.Row
                    for link in vault_packet['tables'].get('knowledge_vault_memory_links', []):
                        atom = conn.execute('SELECT id,scope_project FROM memory_atoms WHERE id=?', (link['atom_id'],)).fetchone()
                        if atom and atom['scope_project'] == link['project']:
                            mutate_memory_action(conn, {'memoryId':atom['id'],'itemType':'atom','action':'disable','reason':'恢复的笔记事项需要重新核对原文与授权'})
            if post_restore is not None:
                # External supervisors use this hook to preserve the durable
                # approval receipt inside the restored database. Keeping the
                # callback in this rollback boundary prevents a successful
                # restore from becoming impossible to reconcile after restart.
                post_restore(target_db)
        except Exception:
            _restore_sqlite(current_snapshot, target_db)
            if vault_before is not None and vault_packet is not None:
                restore_records(knowledge_database, vault_before)
            _restore_optional_files(rime_backups)
            _restore_optional_files(provider_backups)
            raise
    return {
        "schemaVersion": "rag-ime.portable-restore-apply.v1",
        "ok": True,
        "path": str(source),
        "rollbackPath": str(rollback_path),
        "databaseCounts": _database_counts(target_db),
        "providerMetadataRestored": restored_provider_slots,
        "requiresRestart": True,
        "secretsChanged": False,
    }


def _configuration_payload(payload: Mapping[str, object]) -> dict[str, object]:
    config, _source = _configuration_payload_with_source(payload)
    return config


def _configuration_hash(config: Mapping[str, object]) -> str:
    encoded = json.dumps(
        config,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _configuration_payload_with_source(
    payload: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    nested = payload.get("config")
    source_path = compact_whitespace(str(payload.get("path") or payload.get("configPath") or ""))
    if isinstance(nested, Mapping) and source_path:
        raise ValueError("configuration must provide either config or path, not both")
    if isinstance(nested, Mapping):
        return dict(nested), {"kind": "inline", "format": "json", "permissionsHardened": False}
    if source_path:
        return load_user_configuration_file(source_path)
    return dict(payload), {"kind": "inline", "format": "json", "permissionsHardened": False}


def load_user_configuration_file(
    path: str | Path,
) -> tuple[dict[str, object], dict[str, object]]:
    source = Path(path).expanduser()
    if source.is_symlink():
        raise ValueError("configuration file must not be a symlink")
    if not source.is_file():
        raise ValueError(f"configuration file not found: {source}")
    suffix = source.suffix.lower()
    if suffix not in {".yaml", ".yml", ".json"}:
        raise ValueError("configuration file must use .yaml, .yml, or .json")
    if source.stat().st_size > _MAX_CONFIG_FILE_BYTES:
        raise ValueError("configuration file is too large")

    mode_before = stat.S_IMODE(source.stat().st_mode)
    permissions_hardened = bool(mode_before & 0o077)
    if permissions_hardened:
        os.chmod(source, 0o600)
    try:
        raw = source.read_text(encoding="utf-8")
        value = json.loads(raw) if suffix == ".json" else yaml.safe_load(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid configuration file: {source.name}") from exc
    if not isinstance(value, Mapping):
        raise ValueError("configuration root must be an object")
    return dict(value), {
        "kind": "file",
        "format": "json" if suffix == ".json" else "yaml",
        "path": str(source),
        "permissionsHardened": permissions_hardened,
        "mode": "0600",
    }


def _validated_settings(settings: Mapping[str, object]) -> tuple[dict[str, object], list[str], list[str]]:
    schema = settings_schema()
    fields = {
        str(field.get("key") or ""): field
        for section in schema.get("sections", [])
        if isinstance(section, Mapping)
        for field in section.get("fields", [])
        if isinstance(field, Mapping) and str(field.get("key") or "")
    }
    defaults = flatten_settings(default_settings())
    flat = flatten_settings(settings)
    normalized: dict[str, object] = {}
    errors: list[str] = []
    unknown: list[str] = []
    for key, value in flat.items():
        field = fields.get(key)
        if field is None or key not in defaults:
            unknown.append(key)
            continue
        default = defaults[key]
        error = _setting_value_error(key, value, default=default, field=field)
        if error:
            errors.append(error)
        else:
            normalized[key] = int(value) if isinstance(default, int) and not isinstance(default, bool) else value
    return unflatten_settings(normalized), errors, sorted(unknown)


def _setting_value_error(
    key: str,
    value: object,
    *,
    default: object,
    field: Mapping[str, object],
) -> str:
    if isinstance(default, bool):
        if not isinstance(value, bool):
            return f"setting {key} must be a boolean"
    elif isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value).is_integer() is False:
            return f"setting {key} must be an integer"
    elif isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"setting {key} must be a number"
    elif isinstance(default, str) and not isinstance(value, str):
        return f"setting {key} must be a string"
    options = field.get("options")
    if isinstance(options, list) and value not in options:
        return f"setting {key} must be one of: {', '.join(str(item) for item in options)}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = field.get("min")
        maximum = field.get("max")
        if isinstance(minimum, (int, float)) and value < minimum:
            return f"setting {key} must be >= {minimum}"
        if isinstance(maximum, (int, float)) and value > maximum:
            return f"setting {key} must be <= {maximum}"
    return ""


def _provider_errors(slot_name: str, slot: Mapping[str, object]) -> list[str]:
    errors: list[str] = []
    if slot_name in {"instant", "knowledge"}:
        unknown = set(slot) - _PROVIDER_KEYS
        errors.extend(f"unknown providers.{slot_name} field: {key}" for key in sorted(unknown))
        if "endpoint" in slot:
            endpoint = compact_whitespace(str(slot.get("endpoint") or ""))
            try:
                parsed = urlsplit(endpoint)
                hostname = parsed.hostname
            except ValueError:
                parsed = None
                hostname = None
            if parsed is None or parsed.scheme.lower() not in {"http", "https"} or not hostname:
                errors.append(
                    f"providers.{slot_name}.endpoint must be a complete http or https URL"
                )
    else:
        allowed = {
            "provider", "appID", "accessToken", "resourceID", "endpoint",
            "model", "headers",
        }
        errors.extend(f"unknown providers.voice field: {key}" for key in sorted(set(slot) - allowed))
        provider = compact_whitespace(str(slot.get("provider") or "native_streaming"))
        if provider not in {"native_streaming", "realtime_websocket", "http_transcription"}:
            errors.append("providers.voice.provider is unsupported")
    return errors


def _apply_model_provider(
    slot_name: str,
    slot: Mapping[str, object],
    *,
    support_directory: Path,
) -> dict[str, object]:
    support_directory.mkdir(parents=True, exist_ok=True)
    path = support_directory / ("predictor.env" if slot_name == "instant" else "deepseek.env")
    existing = _read_env(path)
    if slot_name == "instant":
        mapping = {
            "RAG_IME_PREDICTOR_PROVIDER": _provider_field(
                slot, "provider", fallback=existing.get("RAG_IME_PREDICTOR_PROVIDER") or "mlx"
            ),
            "RAG_IME_PREDICTOR_BASE_URL": _provider_field(
                slot,
                "endpoint",
                fallback=existing.get("RAG_IME_PREDICTOR_BASE_URL") or "http://127.0.0.1:8767",
            ),
            "RAG_IME_PREDICTOR_MODEL": _provider_field(
                slot, "model", fallback=existing.get("RAG_IME_PREDICTOR_MODEL") or ""
            ),
            "RAG_IME_PREDICTOR_EXTRA_HEADERS_JSON": _headers_text(slot.get("headers"), fallback=existing.get("RAG_IME_PREDICTOR_EXTRA_HEADERS_JSON", "")),
        }
        secret_account = MODEL_INSTANT_ACCOUNT
        legacy_secret_names = {"RAG_IME_PREDICTOR_API_KEY"}
    else:
        mapping = {
            "RAG_IME_KNOWLEDGE_PROVIDER": _provider_field(
                slot,
                "provider",
                fallback=existing.get("RAG_IME_KNOWLEDGE_PROVIDER") or "deepseek",
            ),
            "RAG_IME_DEEPSEEK_BASE_URL": _provider_field(
                slot,
                "endpoint",
                fallback=existing.get("RAG_IME_DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
            ),
            "RAG_IME_DEEPSEEK_MODEL": _provider_field(
                slot,
                "model",
                fallback=existing.get("RAG_IME_DEEPSEEK_MODEL") or "deepseek-v4-flash",
            ),
            "RAG_IME_KNOWLEDGE_EXTRA_HEADERS_JSON": _headers_text(slot.get("headers"), fallback=existing.get("RAG_IME_KNOWLEDGE_EXTRA_HEADERS_JSON", "")),
            "RAG_IME_DEEPSEEK_ACTIVE_RAG": "1",
        }
        secret_account = MODEL_KNOWLEDGE_ACCOUNT
        legacy_secret_names = {"RAG_IME_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"}
    provided_secret = compact_whitespace(str(slot.get("apiKey") or ""))
    if provided_secret:
        write_keychain_secret(MODEL_KEYCHAIN_SERVICE, secret_account, provided_secret)
        existing = {key: value for key, value in existing.items() if key not in legacy_secret_names}
    _write_env(path, {**existing, **mapping})
    return {
        "configured": True,
        "path": str(path),
        "secretImportedToKeychain": bool(provided_secret),
        "existingSecretPreserved": not bool(provided_secret),
    }


def _provider_field(slot: Mapping[str, object], key: str, *, fallback: str) -> str:
    if key not in slot:
        return compact_whitespace(fallback)
    return compact_whitespace(str(slot.get(key) or ""))


def _apply_voice_provider(slot: Mapping[str, object], *, support_directory: Path) -> dict[str, object]:
    provider = compact_whitespace(str(slot.get("provider") or "native_streaming")) or "native_streaming"
    service = "com.rag-ime.voice.volcengine" if provider == "native_streaming" else f"com.rag-ime.voice.{provider}"
    values = {
        "app-id": compact_whitespace(str(slot.get("appID") or "")),
        "access-token": compact_whitespace(str(slot.get("accessToken") or "")),
        "resource-id": compact_whitespace(str(slot.get("resourceID") or "")),
        "endpoint": compact_whitespace(str(slot.get("endpoint") or "")),
        "model": compact_whitespace(str(slot.get("model") or "")),
        "headers-json": _headers_text(slot.get("headers"), fallback=""),
    }
    imported = 0
    for account, value in values.items():
        if value:
            write_keychain_secret(service, account, value)
            imported += 1
    support_directory.mkdir(parents=True, exist_ok=True)
    provider_path = support_directory / "voice-provider.json"
    _write_json(
        provider_path,
        {"schemaVersion": "rag-ime.voice-provider.v1", "provider": provider},
    )
    os.chmod(provider_path, 0o600)
    return {
        "configured": True,
        "provider": provider,
        "secretFieldsImportedToKeychain": imported,
        "existingSecretPreserved": not bool(values["access-token"]),
    }


def provider_metadata(*, support_directory: str | Path | None = None) -> dict[str, object]:
    support = (
        Path(support_directory).expanduser()
        if support_directory is not None
        else Path.home() / "Library" / "Application Support" / "RagIme"
    )
    return _provider_metadata(support)


def _provider_metadata(support_directory: Path) -> dict[str, object]:
    instant = _read_env(support_directory / "predictor.env")
    knowledge = _read_env(support_directory / "deepseek.env")
    voice = {}
    voice_path = support_directory / "voice-provider.json"
    if voice_path.exists():
        try:
            voice_value = json.loads(voice_path.read_text(encoding="utf-8"))
            voice = dict(voice_value) if isinstance(voice_value, Mapping) else {}
        except (OSError, json.JSONDecodeError):
            voice = {}
    return {
        "schemaVersion": "rag-ime.provider-metadata.v1",
        "instant": {
            "provider": instant.get("RAG_IME_PREDICTOR_PROVIDER", ""),
            "endpoint": _safe_endpoint_metadata(instant.get("RAG_IME_PREDICTOR_BASE_URL", "")),
            "model": instant.get("RAG_IME_PREDICTOR_MODEL", ""),
        },
        "knowledge": {
            "provider": knowledge.get("RAG_IME_KNOWLEDGE_PROVIDER", ""),
            "endpoint": _safe_endpoint_metadata(knowledge.get("RAG_IME_DEEPSEEK_BASE_URL", "")),
            "model": knowledge.get("RAG_IME_DEEPSEEK_MODEL", ""),
        },
        "voice": {"provider": compact_whitespace(str(voice.get("provider") or ""))},
        "secretsIncluded": False,
    }


def _rime_configuration_files(root: Path) -> list[Path]:
    return _rime_configuration_selection(root)[0]


def _rime_configuration_selection(root: Path) -> tuple[list[Path], list[str]]:
    if not root.exists():
        return [], []
    result: list[Path] = []
    skipped: list[str] = []
    for path in sorted(root.glob("*.yaml")):
        if not path.is_file() or path.is_symlink() or path.stat().st_size > _MAX_RIME_FILE_BYTES:
            continue
        name = path.name.lower()
        if name.endswith(".custom.yaml") or name in {"rag_ime_user.dict.yaml", "user.yaml"}:
            if _configuration_file_contains_secret(path):
                skipped.append(path.name)
                continue
            result.append(path)
    return result[:128], skipped


_SECRET_YAML_KEY_RE = re.compile(
    r"^\s*(?:api[_-]?key|access[_-]?token|authorization|password|secret)\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(r"(?:\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{12,})", re.IGNORECASE)


def _configuration_file_contains_secret(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return True
    if _SECRET_VALUE_RE.search(text):
        return True
    for line in text.splitlines():
        match = _SECRET_YAML_KEY_RE.match(line)
        if match is None:
            continue
        value = match.group(1).strip().strip("'\"")
        if value and value.lower() not in {"null", "none", "<redacted>"} and not value.startswith("${"):
            return True
    return False


def _safe_endpoint_metadata(value: object) -> str:
    endpoint = compact_whitespace(str(value or ""))
    if not endpoint:
        return ""
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        return ""
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return ""
    host = hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))


def _read_json_file(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid backup JSON: {path.name}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"invalid backup JSON: {path.name}")
    return dict(value)


def _restore_provider_metadata(
    metadata: Mapping[str, object],
    *,
    support_directory: Path,
) -> list[str]:
    restored: list[str] = []
    for slot_name in ("instant", "knowledge"):
        raw = metadata.get(slot_name)
        if not isinstance(raw, Mapping):
            continue
        slot = {
            key: raw.get(key)
            for key in ("provider", "endpoint", "model")
            if compact_whitespace(str(raw.get(key) or ""))
        }
        if not slot:
            continue
        _apply_model_provider(slot_name, slot, support_directory=support_directory)
        restored.append(slot_name)
    voice = metadata.get("voice")
    if isinstance(voice, Mapping):
        provider = compact_whitespace(str(voice.get("provider") or ""))
        if provider:
            _apply_voice_provider({"provider": provider}, support_directory=support_directory)
            restored.append("voice")
    return restored


def _snapshot_optional_file(source: Path, backup: Path) -> Path | None:
    if not source.exists():
        return None
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"refusing to replace non-regular configuration file: {source}")
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, backup)
    return backup


def _restore_optional_files(backups: Mapping[Path, Path | None]) -> None:
    for destination, backup in backups.items():
        if backup is None:
            if destination.exists() or destination.is_symlink():
                destination.unlink()
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.rollback-tmp")
        shutil.copy2(backup, temporary)
        temporary.replace(destination)


def _backup_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    target_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(target_conn)
        target_conn.commit()
        target_conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        target_conn.close()
        source_conn.close()


def _restore_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    target_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()


def _database_counts(path: Path) -> dict[str, int]:
    tables = ("input_events", "memory_items", "memory_books", "memory_atoms", "planning_tasks", "planning_goals")
    with sqlite_connection(f"file:{path}?mode=ro&immutable=1", uri=True) as conn:
        available = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) if table in available else 0
            for table in tables
        }


def _database_migration_version(path: Path) -> int:
    with sqlite_connection(f"file:{path}?mode=ro&immutable=1", uri=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if exists is None:
            return 0
        row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
        return int(row[0] or 0)


def _remove_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            sidecar.unlink()


def _manifest_entries(staging: Path) -> dict[str, str]:
    return {
        path.relative_to(staging).as_posix(): _sha256_file(path)
        for path in sorted(item for item in staging.rglob("*") if item.is_file())
        if path.name != "manifest.json"
    }


def _validate_archive_members(archive: zipfile.ZipFile) -> None:
    total = 0
    names: set[str] = set()
    for info in archive.infolist():
        name = PurePosixPath(info.filename)
        if name.is_absolute() or ".." in name.parts or "" in name.parts:
            raise ValueError(f"unsafe backup entry: {info.filename}")
        if info.filename in names:
            raise ValueError(f"duplicate backup entry: {info.filename}")
        names.add(info.filename)
        total += max(0, int(info.file_size))
        if total > _MAX_ARCHIVE_BYTES:
            raise ValueError("backup is too large")


def _read_archive_json(archive: zipfile.ZipFile, name: str) -> dict[str, object]:
    try:
        payload = json.loads(archive.read(name).decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid backup JSON: {name}") from exc
    return dict(payload) if isinstance(payload, Mapping) else {}


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip("'\"")
    return result


def _write_env(path: Path, values: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(
        f"{key}={str(values[key]).replace(chr(10), ' ').replace(chr(13), ' ').strip()}"
        for key in sorted(values)
        if compact_whitespace(key)
    ) + "\n"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


def _headers_text(value: object, *, fallback: str) -> str:
    if value in (None, ""):
        return fallback
    if isinstance(value, Mapping):
        return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raw = compact_whitespace(str(value))
    if not raw:
        return fallback
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("provider headers must be a JSON object") from exc
    if not isinstance(parsed, Mapping):
        raise ValueError("provider headers must be a JSON object")
    return json.dumps(dict(parsed), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
