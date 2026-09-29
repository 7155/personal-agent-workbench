from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .config_portability import restore_portable_backup
from .db import sqlite_connection
from .settings_store import ManagementSettingsStore


PORTABLE_RESTORE_ACTION = "restore_backup"
PORTABLE_RESTORE_PLAN_SCHEMA = "rag-ime.external-restore-plan.v1"
PORTABLE_RESTORE_RESULT_SCHEMA = "rag-ime.external-restore-result.v1"
_PLAN_ID_PATTERN = re.compile(r"^restore-[0-9a-f]{24}$")


def app_support_directory() -> Path:
    configured = str(os.environ.get("RAG_IME_APP_SUPPORT_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / "Library" / "Application Support" / "RagIme").resolve()


def external_action_paths(
    plan_id: str,
    *,
    support_directory: str | Path | None = None,
) -> tuple[Path, Path]:
    normalized = str(plan_id or "").strip()
    if not _PLAN_ID_PATTERN.fullmatch(normalized):
        raise ValueError("external restore plan id is invalid")
    support = (
        Path(support_directory).expanduser().resolve()
        if support_directory is not None
        else app_support_directory()
    )
    root = support / "Agent" / "external-actions"
    return root / f"{normalized}.json", root / f"{normalized}.result.json"


def materialize_portable_restore_plan(
    *,
    approval: Mapping[str, object],
    session: Mapping[str, object],
    pending_receipt: Mapping[str, object],
    origin_process_id: int,
) -> dict[str, object]:
    receipt = dict(pending_receipt)
    private_value = receipt.pop("_externalPlanPayload", None)
    if not isinstance(private_value, Mapping):
        raise ValueError("portable restore approval is missing its private plan payload")
    private = dict(private_value)
    if str(receipt.get("externalAction") or "") != PORTABLE_RESTORE_ACTION:
        raise ValueError("portable restore external action is invalid")

    approval_id = str(approval.get("approvalId") or "").strip()
    session_id = str(approval.get("sessionId") or "").strip()
    if not approval_id or not session_id or session_id != str(session.get("id") or ""):
        raise ValueError("portable restore approval/session identity is invalid")
    plan_id = f"restore-{hashlib.sha256(approval_id.encode('utf-8')).hexdigest()[:24]}"

    support = Path(str(private.get("supportDirectory") or "")).expanduser().resolve()
    if support != app_support_directory():
        raise ValueError("portable restore support directory changed after preview")
    archive = Path(str(private.get("archivePath") or "")).expanduser().resolve()
    backup_root = (support / "Backups").resolve()
    if archive.parent != backup_root or archive.suffix != ".ragime-backup":
        raise ValueError("portable restore archive escaped the managed backup directory")
    if not archive.is_file() or archive.is_symlink():
        raise ValueError("portable restore archive is no longer a regular managed file")
    database = Path(str(private.get("databasePath") or "")).expanduser().resolve()
    if database.parent != support or database.name != "rag-ime.sqlite" or database.is_symlink():
        raise ValueError("portable restore database is outside the managed product directory")
    restore_token = str(private.get("restoreToken") or "").strip()
    if len(restore_token) != 64 or any(char not in "0123456789abcdef" for char in restore_token):
        raise ValueError("portable restore token is invalid")

    plan_path, result_path = external_action_paths(plan_id, support_directory=support)
    command = ["rag-ime-supervisor", PORTABLE_RESTORE_ACTION, plan_id]
    receipt.update(
        {
            "originProcessId": int(origin_process_id),
            "externalCommand": command,
            "externalCommandSha256": _sha256_json(command),
            "externalPlanId": plan_id,
        }
    )
    plan = {
        "schemaVersion": PORTABLE_RESTORE_PLAN_SCHEMA,
        "planId": plan_id,
        "createdAtMs": int(time.time() * 1000),
        "approval": _json_object(approval),
        "session": _json_object(session),
        "pendingReceipt": _json_object(receipt),
        "restore": {
            "archivePath": str(archive),
            "databasePath": str(database),
            "restoreToken": restore_token,
            "confirmText": "RESTORE RAG-IME",
            "rimeUserDirectory": str(private.get("rimeUserDirectory") or ""),
            "supportDirectory": str(support),
            "launchAgentPlist": str(private.get("launchAgentPlist") or ""),
            "launchAgentTarget": str(private.get("launchAgentTarget") or ""),
        },
        "resultPath": str(result_path),
    }
    _write_private_json(plan_path, plan)
    plan_sha256 = _sha256_file(plan_path)
    receipt["externalPlanSha256"] = plan_sha256
    return receipt


def load_external_action_result(
    *,
    plan_id: str,
    plan_sha256: str,
    support_directory: str | Path | None = None,
) -> dict[str, object]:
    plan_path, result_path = external_action_paths(plan_id, support_directory=support_directory)
    if not plan_path.is_file() or plan_path.is_symlink():
        raise ValueError("external restore plan is unavailable")
    if _sha256_file(plan_path) != str(plan_sha256 or ""):
        raise ValueError("external restore plan changed after native approval")
    result = _read_json(result_path)
    if result.get("schemaVersion") != PORTABLE_RESTORE_RESULT_SCHEMA:
        raise ValueError("external restore result schema is invalid")
    if result.get("planId") != plan_id or result.get("planSha256") != plan_sha256:
        raise ValueError("external restore result does not match the approved plan")
    return result


def execute_portable_restore_plan(
    plan_path: str | Path,
    *,
    command_runner: Callable[[list[str]], Any] | None = None,
    process_alive: Callable[[int], bool] | None = None,
) -> dict[str, object]:
    source = Path(plan_path).expanduser().resolve()
    plan = _read_json(source)
    if plan.get("schemaVersion") != PORTABLE_RESTORE_PLAN_SCHEMA:
        raise ValueError("unsupported external restore plan schema")
    plan_id = str(plan.get("planId") or "")
    restore = plan.get("restore") if isinstance(plan.get("restore"), Mapping) else {}
    support = Path(str(restore.get("supportDirectory") or "")).expanduser().resolve()
    expected_plan, expected_result = external_action_paths(plan_id, support_directory=support)
    if source != expected_plan.resolve() or source.is_symlink():
        raise ValueError("external restore plan is outside its managed directory")
    if source.stat().st_mode & 0o077:
        raise ValueError("external restore plan permissions are too broad")
    plan_sha256 = _sha256_file(source)
    if Path(str(plan.get("resultPath") or "")).expanduser().resolve() != expected_result.resolve():
        raise ValueError("external restore result path is invalid")

    approval = plan.get("approval") if isinstance(plan.get("approval"), Mapping) else {}
    session = plan.get("session") if isinstance(plan.get("session"), Mapping) else {}
    pending_receipt = (
        dict(plan.get("pendingReceipt"))
        if isinstance(plan.get("pendingReceipt"), Mapping)
        else {}
    )
    pending_receipt["externalPlanSha256"] = plan_sha256
    origin_process_id = int(pending_receipt.get("originProcessId") or 0)
    target = str(restore.get("launchAgentTarget") or "")
    plist = Path(str(restore.get("launchAgentPlist") or "")).expanduser().resolve()
    expected_target = f"gui/{os.getuid()}/com.rag-ime.sidecar"
    expected_plist = (Path.home() / "Library" / "LaunchAgents" / "com.rag-ime.sidecar.plist").resolve()
    if target != expected_target or plist != expected_plist:
        raise ValueError("external restore launch agent identity is invalid")

    runner = command_runner or _run_command
    alive = process_alive or _process_alive
    result: dict[str, object] = {
        "schemaVersion": PORTABLE_RESTORE_RESULT_SCHEMA,
        "planId": plan_id,
        "planSha256": plan_sha256,
        "approvalId": str(approval.get("approvalId") or ""),
        "restoreApplied": False,
        "restartRequested": False,
        "ok": False,
        "completedAtMs": 0,
    }

    bootout = runner(["/bin/launchctl", "bootout", target])
    bootout_code = int(getattr(bootout, "returncode", 1))
    if bootout_code != 0 and origin_process_id > 0 and alive(origin_process_id):
        result.update(
            {
                "error": "Sidecar could not be stopped before database restore",
                "bootoutExitCode": bootout_code,
                "completedAtMs": int(time.time() * 1000),
            }
        )
        _write_private_json(expected_result, result)
        return result

    try:
        restored = restore_portable_backup(
            archive_path=str(restore.get("archivePath") or ""),
            db_path=str(restore.get("databasePath") or ""),
            settings_store=ManagementSettingsStore(str(restore.get("databasePath") or "")),
            restore_token=str(restore.get("restoreToken") or ""),
            confirm_text=str(restore.get("confirmText") or ""),
            rime_user_dir=str(restore.get("rimeUserDirectory") or "") or None,
            support_directory=support,
            post_restore=lambda database: _preserve_control_records(
                database,
                session=session,
                approval=approval,
                pending_receipt=pending_receipt,
            ),
        )
        result.update(
            {
                "restoreApplied": True,
                "databaseCounts": _json_object(restored.get("databaseCounts")),
                "providerMetadataRestored": [
                    str(value) for value in restored.get("providerMetadataRestored", [])
                ],
                "rollbackFileName": Path(str(restored.get("rollbackPath") or "")).name,
                "secretsChanged": restored.get("secretsChanged") is True,
            }
        )
    except Exception as exc:
        result["error"] = _bounded_error(exc)
    finally:
        bootstrap = runner(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])
        result["bootstrapExitCode"] = int(getattr(bootstrap, "returncode", 1))
        result["restartRequested"] = result["bootstrapExitCode"] == 0

    result["ok"] = result.get("restoreApplied") is True and result.get("restartRequested") is True
    if result.get("restoreApplied") is True and result.get("restartRequested") is not True:
        result["error"] = "Database restore completed, but Sidecar could not be restarted"
    result["completedAtMs"] = int(time.time() * 1000)
    _write_private_json(expected_result, result)
    return result


def _preserve_control_records(
    database: Path,
    *,
    session: Mapping[str, object],
    approval: Mapping[str, object],
    pending_receipt: Mapping[str, object],
) -> None:
    session_id = str(session.get("id") or "")
    if not session_id or session_id != str(approval.get("sessionId") or ""):
        raise ValueError("external restore control record identity is invalid")
    roots = session.get("workspaceRoots") if isinstance(session.get("workspaceRoots"), list) else []
    with sqlite_connection(database, foreign_keys=True) as conn:
        conn.execute(
            """
            INSERT INTO agent_sessions(
                id, pi_session_id, session_file, title, session_mode, role_id, role_version,
                model_profile, tool_profile_version, workspace_roots_json, shell_policy_version,
                created_at_ms, updated_at_ms, last_opened_at_ms, status, archived_at_ms,
                message_count, last_message_preview, cache_read_tokens, cache_write_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                pi_session_id = excluded.pi_session_id,
                session_file = excluded.session_file,
                title = excluded.title,
                session_mode = excluded.session_mode,
                role_id = excluded.role_id,
                role_version = excluded.role_version,
                model_profile = excluded.model_profile,
                tool_profile_version = excluded.tool_profile_version,
                workspace_roots_json = excluded.workspace_roots_json,
                shell_policy_version = excluded.shell_policy_version,
                updated_at_ms = excluded.updated_at_ms,
                last_opened_at_ms = excluded.last_opened_at_ms,
                status = excluded.status,
                archived_at_ms = excluded.archived_at_ms,
                message_count = excluded.message_count,
                last_message_preview = excluded.last_message_preview,
                cache_read_tokens = excluded.cache_read_tokens,
                cache_write_tokens = excluded.cache_write_tokens
            """,
            (
                session_id,
                str(session.get("piSessionId") or ""),
                str(session.get("sessionFile") or ""),
                str(session.get("title") or "恢复后的 Agent 会话")[:120],
                str(session.get("mode") or "assistant"),
                str(session.get("roleId") or "companion-present-v1"),
                str(session.get("roleVersion") or "1"),
                str(session.get("modelProfile") or "deepseek-v4"),
                str(session.get("toolProfileVersion") or "control-center-v1"),
                json.dumps([str(value) for value in roots], ensure_ascii=False, separators=(",", ":")),
                str(session.get("shellPolicyVersion") or "assistant-no-shell-v1"),
                int(session.get("createdAtMs") or 0),
                int(time.time() * 1000),
                int(session.get("lastOpenedAtMs") or 0),
                "idle",
                session.get("archivedAtMs"),
                int(session.get("messageCount") or 0),
                str(session.get("lastMessagePreview") or "")[:240],
                int(session.get("cacheReadTokens") or 0),
                int(session.get("cacheWriteTokens") or 0),
            ),
        )
        conn.execute(
            """
            INSERT INTO agent_approvals(
                approval_id, session_id, tool_name, operation, payload_sha256, preview_json,
                risk_level, state, requested_at_ms, expires_at_ms, decided_at_ms,
                decided_by, receipt_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'external_pending', ?, ?, ?, ?, ?)
            ON CONFLICT(approval_id) DO UPDATE SET
                state = 'external_pending',
                decided_at_ms = excluded.decided_at_ms,
                decided_by = excluded.decided_by,
                receipt_json = excluded.receipt_json
            """,
            (
                str(approval.get("approvalId") or ""),
                session_id,
                str(approval.get("toolId") or ""),
                str(approval.get("operation") or ""),
                str(approval.get("payloadSha256") or ""),
                json.dumps(_json_object(approval.get("preview")), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                str(approval.get("riskLevel") or "R3"),
                int(approval.get("requestedAtMs") or 0),
                int(approval.get("expiresAtMs") or 0),
                int(approval.get("decidedAtMs") or int(time.time() * 1000)),
                "native-control-center",
                json.dumps(_json_object(pending_receipt), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ),
        )


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, timeout=30, check=False)


def _process_alive(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _write_private_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"external action file is unavailable: {path.name}")
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("external action file is too large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("external action file must contain an object")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _json_object(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    loaded = json.loads(encoded)
    return loaded if isinstance(loaded, dict) else {}


def _bounded_error(error: object) -> str:
    return " ".join(str(error).split())[:500] or "external restore failed"
