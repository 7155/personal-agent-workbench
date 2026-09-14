from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable


MIGRATION_NAME_RE = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]+)\.sql$")
DEFAULT_MIGRATIONS_DIR = Path(__file__).with_name("migrations")


class MigrationChecksumError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str


@dataclass(frozen=True)
class MigrationResult:
    applied_versions: tuple[int, ...]
    current_version: int
    migration_count: int

    def payload(self) -> dict[str, object]:
        return {
            "schemaVersion": "rag-ime.sqlite-migrations.v1",
            "appliedVersions": list(self.applied_versions),
            "currentVersion": self.current_version,
            "migrationCount": self.migration_count,
        }


MigrationHook = Callable[[sqlite3.Connection, int], None]


def apply_database_migrations(
    conn: sqlite3.Connection,
    *,
    migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR,
    applied_at_ms: int | None = None,
) -> MigrationResult:
    # A caller that already opened a transaction (the team identity store uses
    # BEGIN IMMEDIATE) owns the migration transaction.  Committing one
    # migration at a time in that case releases SQLite's writer lock between
    # migrations while this function still holds a stale `applied` snapshot;
    # another initializer can then apply the next migration first.  Keep the
    # caller's transaction open so schema inspection and every migration are
    # serialized as one unit.  Callers without an active transaction retain
    # the historical per-migration commit behavior.
    caller_transaction_open = conn.in_transaction
    resolved_migrations_dir = Path(migrations_dir).resolve(strict=False)
    migrations = load_migrations(resolved_migrations_dir)
    default_migration_names = {
        migration.version: migration.name for migration in _load_default_migrations()
    }
    _ensure_migration_table(conn)
    applied = {
        int(row[0]): str(row[1])
        for row in conn.execute("SELECT version, checksum FROM schema_migrations ORDER BY version")
    }
    for migration in migrations:
        existing_checksum = applied.get(migration.version)
        if existing_checksum is not None and existing_checksum != migration.checksum:
            raise MigrationChecksumError(
                f"migration {migration.version:04d} checksum changed: "
                f"database={existing_checksum} source={migration.checksum}"
            )

    newly_applied: list[int] = []
    timestamp = int(time.time() * 1000) if applied_at_ms is None else int(applied_at_ms)
    for migration in migrations:
        if migration.version in applied:
            continue
        # Hooks belong to the packaged PAW schema.  Test/deployment fixtures
        # may copy or deliberately alter a packaged migration in a custom
        # directory, so retain the source migration name as its identity.
        # Team and other isolated stores intentionally reuse version numbers
        # with different names and must never inherit a hook by coincidence.
        hook = (
            _MIGRATION_HOOKS.get(migration.version)
            if default_migration_names.get(migration.version) == migration.name
            else None
        )
        if caller_transaction_open:
            # Hooks are allowed to manage a transaction themselves for legacy
            # migrations.  If one commits the caller's transaction, reacquire
            # the writer lock before recording the migration row; team schema
            # migrations have no such hooks and stay under the original lock.
            if hook is not None:
                hook(conn, timestamp)
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            if migration.sql.strip():
                _execute_sql_script(conn, migration.sql)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at_ms, checksum) VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, timestamp, migration.checksum),
            )
        else:
            with conn:
                if hook is not None:
                    hook(conn, timestamp)
                if migration.sql.strip():
                    _execute_sql_script(conn, migration.sql)
                conn.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at_ms, checksum) VALUES (?, ?, ?, ?)",
                    (migration.version, migration.name, timestamp, migration.checksum),
                )
        newly_applied.append(migration.version)
        applied[migration.version] = migration.checksum
    return MigrationResult(
        applied_versions=tuple(newly_applied),
        current_version=max(applied, default=0),
        migration_count=len(migrations),
    )


def migration_status(
    conn: sqlite3.Connection,
    *,
    migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR,
) -> dict[str, object]:
    migrations = load_migrations(migrations_dir)
    _ensure_migration_table(conn)
    rows = conn.execute(
        "SELECT version, name, applied_at_ms, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    source = {migration.version: migration for migration in migrations}
    items = [
        {
            "version": int(row[0]),
            "name": str(row[1]),
            "appliedAtMs": int(row[2]),
            "checksum": str(row[3]),
            "sourceChecksum": source[int(row[0])].checksum if int(row[0]) in source else "",
            "checksumMatches": int(row[0]) in source and str(row[3]) == source[int(row[0])].checksum,
        }
        for row in rows
    ]
    applied_versions = {int(row[0]) for row in rows}
    return {
        "schemaVersion": "rag-ime.sqlite-migration-status.v1",
        "currentVersion": max(applied_versions, default=0),
        "latestVersion": max(source, default=0),
        "pendingVersions": [item.version for item in migrations if item.version not in applied_versions],
        "items": items,
        "ok": all(bool(item["checksumMatches"]) for item in items),
    }


def latest_migration_version(
    migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR,
) -> int:
    """Return the source-owned schema head without opening a database."""
    return max((migration.version for migration in load_migrations(migrations_dir)), default=0)


def load_migrations(migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR) -> tuple[Migration, ...]:
    root = Path(migrations_dir).resolve(strict=False)
    if root == DEFAULT_MIGRATIONS_DIR.resolve(strict=False):
        return _load_default_migrations()
    return _read_migrations(root)


@lru_cache(maxsize=1)
def _load_default_migrations() -> tuple[Migration, ...]:
    """Read packaged migration sources once for this process generation.

    Every Store still verifies the live database's applied versions and
    checksums.  Only the immutable SQL resource reads are shared, avoiding
    thousands of identical filesystem reads during Gateway construction.
    """

    return _read_migrations(DEFAULT_MIGRATIONS_DIR.resolve(strict=False))


def clear_migration_source_cache() -> None:
    """Forget packaged migration resources after a deliberate replacement."""

    _load_default_migrations.cache_clear()


def _read_migrations(root: Path) -> tuple[Migration, ...]:
    migrations: list[Migration] = []
    seen_versions: set[int] = set()
    for path in sorted(root.glob("*.sql")):
        match = MIGRATION_NAME_RE.match(path.name)
        if match is None:
            continue
        version = int(match.group("version"))
        if version in seen_versions:
            raise ValueError(f"duplicate migration version: {version:04d}")
        seen_versions.add(version)
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        migrations.append(
            Migration(
                version=version,
                name=match.group("name"),
                path=path,
                sql=sql,
                checksum=checksum,
            )
        )
    return tuple(migrations)


def _ensure_migration_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at_ms INTEGER NOT NULL,
            checksum TEXT NOT NULL
        )
        """
    )


def _execute_sql_script(conn: sqlite3.Connection, sql: str) -> None:
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if not sqlite3.complete_statement(statement):
            continue
        if statement.strip():
            conn.execute(statement)
        statement = ""
    remaining = "\n".join(
        line for line in statement.splitlines() if not line.strip().startswith("--")
    ).strip()
    if remaining:
        raise ValueError("incomplete SQL migration statement")


def _canonicalize_memory_feedback_events(
    conn: sqlite3.Connection,
    _applied_at_ms: int,
) -> None:
    if not _table_exists(conn, "memory_feedback_events"):
        return
    columns = {str(row[1]): row for row in conn.execute("PRAGMA table_info(memory_feedback_events)")}
    candidate_text = columns.get("candidate_text")
    candidate_source = columns.get("candidate_source")
    if candidate_text is None or candidate_source is None:
        raise RuntimeError("memory_feedback_events is missing candidate columns")
    if str(candidate_text[4] or "") == "''" and str(candidate_source[4] or "") == "'unknown'":
        return
    legacy = "memory_feedback_events_legacy_0001"
    conn.execute(f"DROP TABLE IF EXISTS {legacy}")
    conn.execute(f"ALTER TABLE memory_feedback_events RENAME TO {legacy}")
    conn.execute(
        """
        CREATE TABLE memory_feedback_events (
            id TEXT PRIMARY KEY,
            candidate_id TEXT,
            candidate_text TEXT NOT NULL DEFAULT '',
            candidate_source TEXT NOT NULL DEFAULT 'unknown',
            action TEXT NOT NULL,
            context_hash TEXT,
            front_app_bundle_id TEXT,
            raw_input TEXT,
            preedit TEXT,
            committed_tail TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at_ms INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        INSERT INTO memory_feedback_events(
            id, candidate_id, candidate_text, candidate_source, action,
            context_hash, front_app_bundle_id, raw_input, preedit,
            committed_tail, metadata_json, created_at_ms
        )
        SELECT id, candidate_id, COALESCE(candidate_text, ''),
               COALESCE(NULLIF(candidate_source, ''), 'unknown'), action,
               context_hash, front_app_bundle_id, raw_input, preedit,
               committed_tail, COALESCE(metadata_json, '{{}}'), created_at_ms
        FROM {legacy}
        """
    )
    conn.execute(f"DROP TABLE {legacy}")


def _migrate_context_group_columns(
    conn: sqlite3.Connection,
    _applied_at_ms: int,
) -> None:
    if not _table_exists(conn, "input_events"):
        return
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(input_events)")}
    if "context_group_id" not in columns:
        conn.execute("ALTER TABLE input_events ADD COLUMN context_group_id TEXT NOT NULL DEFAULT ''")
    if "context_group_level" not in columns:
        conn.execute("ALTER TABLE input_events ADD COLUMN context_group_level TEXT NOT NULL DEFAULT 'app'")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_input_events_context_group "
        "ON input_events(context_group_id, created_at_ms DESC)"
    )


def _canonicalize_input_events(
    conn: sqlite3.Connection,
    _applied_at_ms: int,
) -> None:
    """Bring early input-event tables up to the legacy-core contract in place."""
    if not _table_exists(conn, "input_events"):
        return
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(input_events)")}
    additions = (
        ("recent_context", "TEXT NOT NULL DEFAULT ''"),
        ("preedit", "TEXT NOT NULL DEFAULT ''"),
        ("schema_id", "TEXT NOT NULL DEFAULT 'default'"),
        ("app", "TEXT NOT NULL DEFAULT 'manual'"),
        ("project", "TEXT NOT NULL DEFAULT ''"),
        ("candidate_rank", "INTEGER"),
        ("provider_name", "TEXT NOT NULL DEFAULT 'local'"),
        ("tags_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("context_group_id", "TEXT NOT NULL DEFAULT ''"),
        ("context_group_level", "TEXT NOT NULL DEFAULT 'app'"),
    )
    for name, declaration in additions:
        if name not in columns:
            conn.execute(f"ALTER TABLE input_events ADD COLUMN {name} {declaration}")


def _migrate_agent_identity_column(
    conn: sqlite3.Connection,
    _applied_at_ms: int,
) -> None:
    """Add the Agent identity column before the identity migration uses it."""

    if not _table_exists(conn, "agent_sessions"):
        return
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(agent_sessions)")}
    if "agent_id" not in columns:
        conn.execute("ALTER TABLE agent_sessions ADD COLUMN agent_id TEXT NOT NULL DEFAULT ''")


_LEGACY_PROJECT_TOOL_IDS = {
    "ime_overview": "overview",
    "ime_input": "input",
    "ime_voice": "voice",
    "ime_planning": "planning",
    "ime_memory": "memory",
    "ime_knowledge": "knowledge",
    "ime_models": "models",
    "ime_runtime": "runtime",
    "ime_configuration": "configuration",
    "ime_agents": "agents",
    "ime_browser": "browser",
    "ime_plugins": "plugins",
}
_LEGACY_CAPABILITY_KEYS = {
    f"tool:{legacy}": f"tool:{canonical}"
    for legacy, canonical in _LEGACY_PROJECT_TOOL_IDS.items()
}


def _migrate_project_tool_ids(
    conn: sqlite3.Connection,
    applied_at_ms: int,
) -> None:
    policy_updates: list[tuple[str, str, str]] = []
    for session_id, allowed_raw, disclosure_raw in conn.execute(
        """
        SELECT session_id, allowed_tools_json, disclosure_preferences_json
        FROM agent_session_tool_policies
        """
    ):
        allowed = _load_json(allowed_raw, field="agent_session_tool_policies.allowed_tools_json")
        if allowed is not None and not isinstance(allowed, list):
            raise RuntimeError(
                "agent_session_tool_policies.allowed_tools_json must be an array or null"
            )
        rewritten_allowed = (
            _rewrite_tool_id_array(
                allowed,
                field="agent_session_tool_policies.allowed_tools_json",
            )
            if isinstance(allowed, list)
            else allowed
        )
        disclosure = _load_json_object(
            disclosure_raw,
            field="agent_session_tool_policies.disclosure_preferences_json",
        )
        rewritten_disclosure = _rewrite_capability_preference_keys(
            disclosure,
            field="agent_session_tool_policies.disclosure_preferences_json",
        )
        if rewritten_allowed != allowed or rewritten_disclosure != disclosure:
            policy_updates.append(
                (
                    _json_text(rewritten_allowed),
                    _json_text(rewritten_disclosure),
                    str(session_id),
                )
            )

    configuration_updates: list[tuple[str, int]] = []
    for singleton_id, configuration_raw in conn.execute(
        "SELECT singleton_id, configuration_json FROM agent_configuration_state"
    ):
        configuration = _load_json_object(
            configuration_raw,
            field="agent_configuration_state.configuration_json",
        )
        rewritten = _rewrite_configuration_capability_preferences(configuration)
        if rewritten != configuration:
            configuration_updates.append((_json_text(rewritten), int(singleton_id)))

    live_bindings_to_revoke: list[tuple[str, str]] = []
    for row in conn.execute(
        """
        SELECT binding.session_id, binding.manifest_id, manifest.payload_json,
               binding.room_binding_json, binding.participant_binding_json
        FROM room_v2_capability_runtime_bindings AS binding
        JOIN room_v2_capability_manifests AS manifest
          ON manifest.manifest_id = binding.manifest_id
        WHERE binding.state IN ('prepared', 'active')
        """
    ):
        documents = (
            _load_json(row[2], field="room_v2_capability_manifests.payload_json"),
            _load_json(
                row[3],
                field="room_v2_capability_runtime_bindings.room_binding_json",
            ),
            _load_json(
                row[4],
                field="room_v2_capability_runtime_bindings.participant_binding_json",
            ),
        )
        if any(_contains_legacy_tool_id(document) for document in documents):
            live_bindings_to_revoke.append((str(row[0]), str(row[1])))

    for allowed_json, disclosure_json, session_id in policy_updates:
        conn.execute(
            """
            UPDATE agent_session_tool_policies
            SET allowed_tools_json = ?, disclosure_preferences_json = ?,
                policy_revision = policy_revision + 1,
                updated_at_ms = MAX(updated_at_ms, ?)
            WHERE session_id = ?
            """,
            (allowed_json, disclosure_json, applied_at_ms, session_id),
        )
    for configuration_json, singleton_id in configuration_updates:
        conn.execute(
            """
            UPDATE agent_configuration_state
            SET revision = revision + 1,
                configuration_json = ?,
                applied_revision = CASE
                    WHEN sync_state = 'synchronized' THEN revision + 1
                    ELSE applied_revision
                END,
                updated_at_ms = MAX(updated_at_ms, ?),
                updated_by = 'tool-id-cutover'
            WHERE singleton_id = ?
            """,
            (configuration_json, applied_at_ms, singleton_id),
        )
    conn.execute(
        """
        UPDATE agent_approvals
        SET state = 'expired',
            expires_at_ms = MIN(expires_at_ms, ?),
            decided_at_ms = ?,
            decided_by = 'tool-id-cutover'
        WHERE state IN ('pending', 'approved', 'external_pending')
          AND tool_name IN (
              'ime_overview', 'ime_input', 'ime_voice', 'ime_planning',
              'ime_memory', 'ime_knowledge', 'ime_models', 'ime_runtime',
              'ime_configuration', 'ime_agents', 'ime_browser', 'ime_plugins'
          )
        """,
        (applied_at_ms, applied_at_ms),
    )
    for session_id, manifest_id in live_bindings_to_revoke:
        conn.execute(
            """
            UPDATE room_v2_capability_runtime_bindings
            SET state = 'revoked',
                capability_epoch = capability_epoch + 1,
                updated_at_ms = MAX(updated_at_ms, ?)
            WHERE session_id = ? AND manifest_id = ?
              AND state IN ('prepared', 'active')
            """,
            (applied_at_ms, session_id, manifest_id),
        )


def _rewrite_configuration_capability_preferences(
    configuration: dict[str, object],
) -> dict[str, object]:
    rewritten = dict(configuration)
    defaults = rewritten.get("sessionDefaults")
    if isinstance(defaults, dict) and "capabilityDisclosurePreferences" in defaults:
        rewritten_defaults = dict(defaults)
        preferences = defaults["capabilityDisclosurePreferences"]
        if not isinstance(preferences, dict):
            raise RuntimeError(
                "agent_configuration_state.configuration_json "
                "sessionDefaults.capabilityDisclosurePreferences must be an object"
            )
        rewritten_defaults["capabilityDisclosurePreferences"] = (
            _rewrite_capability_preference_keys(
                preferences,
                field=(
                    "agent_configuration_state.configuration_json "
                    "sessionDefaults.capabilityDisclosurePreferences"
                ),
            )
        )
        rewritten["sessionDefaults"] = rewritten_defaults

    disclosure = rewritten.get("capabilityDisclosure")
    if isinstance(disclosure, dict) and "projectPreferences" in disclosure:
        project_preferences = disclosure["projectPreferences"]
        if not isinstance(project_preferences, dict):
            raise RuntimeError(
                "agent_configuration_state.configuration_json "
                "capabilityDisclosure.projectPreferences must be an object"
            )
        rewritten_projects: dict[str, object] = {}
        for project_id, preferences in project_preferences.items():
            if not isinstance(preferences, dict):
                raise RuntimeError(
                    "agent_configuration_state.configuration_json "
                    f"capabilityDisclosure.projectPreferences[{project_id!r}] "
                    "must be an object"
                )
            rewritten_projects[project_id] = _rewrite_capability_preference_keys(
                preferences,
                field=(
                    "agent_configuration_state.configuration_json "
                    f"capabilityDisclosure.projectPreferences[{project_id!r}]"
                ),
            )
        rewritten_disclosure = dict(disclosure)
        rewritten_disclosure["projectPreferences"] = rewritten_projects
        rewritten["capabilityDisclosure"] = rewritten_disclosure
    return rewritten


def _rewrite_tool_id_array(values: list[object], *, field: str) -> list[object]:
    present = {value for value in values if isinstance(value, str)}
    for legacy, canonical in _LEGACY_PROJECT_TOOL_IDS.items():
        if legacy in present and canonical in present:
            raise RuntimeError(
                f"{field} contains conflicting Tool IDs {legacy!r} and {canonical!r}"
            )
    return [
        _LEGACY_PROJECT_TOOL_IDS.get(value, value)
        if isinstance(value, str)
        else value
        for value in values
    ]


def _rewrite_capability_preference_keys(
    preferences: dict[str, object],
    *,
    field: str,
) -> dict[str, object]:
    for legacy, canonical in _LEGACY_CAPABILITY_KEYS.items():
        if legacy in preferences and canonical in preferences:
            raise RuntimeError(
                f"{field} contains conflicting capability keys "
                f"{legacy!r} and {canonical!r}"
            )
    return {
        _LEGACY_CAPABILITY_KEYS.get(key, key): value
        for key, value in preferences.items()
    }


def _contains_legacy_tool_id(value: object) -> bool:
    if isinstance(value, str):
        return value in _LEGACY_PROJECT_TOOL_IDS or value in _LEGACY_CAPABILITY_KEYS
    if isinstance(value, list):
        return any(_contains_legacy_tool_id(item) for item in value)
    if isinstance(value, dict):
        return any(
            _contains_legacy_tool_id(key) or _contains_legacy_tool_id(item)
            for key, item in value.items()
        )
    return False


def _load_json(raw: object, *, field: str) -> object:
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{field} contains invalid JSON") from error


def _load_json_object(raw: object, *, field: str) -> dict[str, object]:
    value = _load_json(raw, field=field)
    if not isinstance(value, dict):
        raise RuntimeError(f"{field} must be a JSON object")
    return value


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone() is not None


def _migrate_agent_room_work_review_columns(
    conn: sqlite3.Connection,
    _applied_at_ms: int,
) -> None:
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(agent_room_work_items)")
    }
    additions = (
        (
            "review_operability_verdict",
            "TEXT NOT NULL DEFAULT '' CHECK ("
            "review_operability_verdict IN ('', 'passed', 'failed', 'unverified'))",
        ),
        (
            "review_requirement_verdict",
            "TEXT NOT NULL DEFAULT '' CHECK ("
            "review_requirement_verdict IN "
            "('', 'satisfied', 'not_satisfied', 'unverified'))",
        ),
        ("review_evidence_refs_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("review_reason", "TEXT NOT NULL DEFAULT ''"),
        ("reviewer_participant_id", "TEXT NOT NULL DEFAULT ''"),
        ("reviewed_at_ms", "INTEGER"),
    )
    for name, declaration in additions:
        if name in columns:
            continue
        conn.execute(
            f"ALTER TABLE agent_room_work_items ADD COLUMN {name} {declaration}"
        )
        columns.add(name)


def _migrate_agent_room_work_proposed_verdicts(
    conn: sqlite3.Connection,
    _applied_at_ms: int,
) -> None:
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(agent_room_work_items)")
    }
    additions = (
        (
            "proposed_operability_verdict",
            "TEXT NOT NULL DEFAULT '' CHECK ("
            "proposed_operability_verdict IN "
            "('', 'passed', 'failed', 'unverified'))",
        ),
        (
            "proposed_requirement_verdict",
            "TEXT NOT NULL DEFAULT '' CHECK ("
            "proposed_requirement_verdict IN "
            "('', 'satisfied', 'not_satisfied', 'unverified'))",
        ),
    )
    for name, declaration in additions:
        if name in columns:
            continue
        conn.execute(
            f"ALTER TABLE agent_room_work_items ADD COLUMN {name} {declaration}"
        )
        columns.add(name)


def _migrate_memory_catalog_run_kind(
    conn: sqlite3.Connection,
    _timestamp: int,
) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'table' AND name = 'memory_cleanup_runs'"
    ).fetchone()
    if row is None or "catalog_consolidation" in str(row[0] or ""):
        return

    if conn.in_transaction:
        conn.commit()
    foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "ALTER TABLE memory_cleanup_runs "
            "RENAME TO memory_cleanup_runs_before_catalog"
        )
        conn.execute(
            """
            CREATE TABLE memory_cleanup_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT UNIQUE NOT NULL,
                created_at_ms INTEGER NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                summary TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                owner_kind TEXT NOT NULL DEFAULT 'user'
                    CHECK (owner_kind IN ('user', 'shared', 'agent', 'session', 'room')),
                owner_id TEXT NOT NULL DEFAULT 'default',
                run_kind TEXT NOT NULL DEFAULT 'legacy'
                    CHECK (run_kind IN (
                        'legacy',
                        'daily_curation',
                        'manual_curation',
                        'dream_insight',
                        'catalog_consolidation'
                    ))
            )
            """
        )
        conn.execute(
            """
            INSERT INTO memory_cleanup_runs(
                id, run_id, created_at_ms, provider, model, status, summary,
                metadata_json, owner_kind, owner_id, run_kind
            )
            SELECT
                id, run_id, created_at_ms, provider, model, status, summary,
                metadata_json, owner_kind, owner_id, run_kind
            FROM memory_cleanup_runs_before_catalog
            """
        )
        conn.execute("DROP TABLE memory_cleanup_runs_before_catalog")
        conn.execute(
            """
            CREATE INDEX idx_memory_cleanup_runs_owner_recent
            ON memory_cleanup_runs(
                owner_kind,
                owner_id,
                run_kind,
                created_at_ms DESC
            )
            """
        )
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                "memory_cleanup_runs migration introduced foreign-key violations"
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        if foreign_keys_enabled:
            conn.execute("PRAGMA foreign_keys = ON")


_MIGRATION_HOOKS: dict[int, MigrationHook] = {
    1: _canonicalize_memory_feedback_events,
    3: _migrate_context_group_columns,
    7: _canonicalize_input_events,
    29: _migrate_agent_identity_column,
    118: _migrate_project_tool_ids,
    160: _migrate_agent_room_work_review_columns,
    162: _migrate_agent_room_work_proposed_verdicts,
    185: _migrate_memory_catalog_run_kind,
}
