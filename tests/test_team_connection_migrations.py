from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from rag_ime.db import apply_database_migrations, sqlite_connection


TEAM_MIGRATIONS = Path(__file__).parents[1] / "rag_ime" / "team" / "migrations"
LEGACY_0008_SHA256 = "95e4c77e36e6ef24dc960de8974978e74f6e9cc08ecfe35e831627d68fde7d57"


class TeamConnectionMigrationTests(unittest.TestCase):
    def test_0008_bytes_remain_the_applied_legacy_migration(self) -> None:
        migration = TEAM_MIGRATIONS / "0008_connections.sql"
        self.assertEqual(hashlib.sha256(migration.read_bytes()).hexdigest(), LEGACY_0008_SHA256)

    def test_0010_adds_account_fence_and_revokes_legacy_active_grants(self) -> None:
        with tempfile.TemporaryDirectory(prefix="paw-team-connection-migrations-") as root_name:
            root = Path(root_name)
            old_migrations = root / "migrations-before-0010"
            old_migrations.mkdir()
            through_0010 = root / 'migrations-through-0010'
            through_0010.mkdir()
            for migration in sorted(TEAM_MIGRATIONS.glob("*.sql")):
                version = int(migration.name.split('_', 1)[0])
                if version < 10:
                    shutil.copyfile(migration, old_migrations / migration.name)
                if version <= 10:
                    shutil.copyfile(migration, through_0010 / migration.name)

            database = root / "team.sqlite3"
            workspace = root / "workspace"
            workspace.mkdir()
            with sqlite_connection(database, foreign_keys=True) as conn:
                legacy_result = apply_database_migrations(
                    conn, migrations_dir=old_migrations, applied_at_ms=1_000
                )
                self.assertEqual(legacy_result.current_version, 9)
                conn.execute(
                    """
                    INSERT INTO team_users(
                        id, username, username_key, password_hash, display_name, role,
                        active, failed_login_count, locked_until_ms,
                        authorization_revision, created_at_ms, updated_at_ms
                    ) VALUES ('usr_legacy', 'legacy', 'legacy', 'test-hash', 'Legacy', 'member',
                              1, 0, 0, 1, 1, 1)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO team_spaces(
                        id, kind, name, owner_user_id, revision, created_at_ms, updated_at_ms
                    ) VALUES ('space_legacy', 'project', 'Legacy project', 'usr_legacy', 1, 1, 1)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO team_project_members(
                        space_id, user_id, role, active, membership_revision, added_at_ms
                    ) VALUES ('space_legacy', 'usr_legacy', 'owner', 1, 1, 1)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO team_session_bindings(
                        session_id, space_id, owner_user_id, membership_revision,
                        workspace_path, audience, generation, active, created_at_ms
                    ) VALUES ('agent:legacy', 'space_legacy', 'usr_legacy', 1, ?, 'project', 0, 1, 1)
                    """,
                    (str(workspace),),
                )
                conn.execute(
                    """
                    INSERT INTO team_connections(
                        id, provider, scope, owner_user_id, space_id, label, account_login,
                        repositories_json, operations_json, status, revision, secret_blob,
                        secret_version, refresh_state, refresh_started_at_ms,
                        created_at_ms, updated_at_ms
                    ) VALUES ('conn_legacy', 'github', 'project', 'usr_legacy', 'space_legacy',
                              'Legacy connection', 'legacy-gh', '["acme/app"]', '["repo.read"]',
                              'active', 1, NULL, 1, 'idle', NULL, 1, 1)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO team_connection_grants(
                        id, connection_id, session_id, space_id, actor_user_id, repository,
                        operations_json, membership_revision, connection_revision,
                        expires_at_ms, status, created_at_ms, revoked_at_ms
                    ) VALUES ('grant_legacy', 'conn_legacy', 'agent:legacy', 'space_legacy',
                              'usr_legacy', 'acme/app', '["repo.read"]', 1, 1, 9_999_999,
                              'active', 1, NULL)
                    """
                )

            with sqlite_connection(database, foreign_keys=True) as conn:
                upgraded = apply_database_migrations(
                    conn, migrations_dir=through_0010, applied_at_ms=2_000
                )
                self.assertEqual(upgraded.applied_versions, (10,))
                columns = {
                    str(row[1]): row for row in conn.execute("PRAGMA table_info(team_connection_grants)")
                }
                self.assertIn("user_authorization_revision", columns)
                row = conn.execute(
                    "SELECT status, user_authorization_revision, revoked_at_ms "
                    "FROM team_connection_grants WHERE id='grant_legacy'"
                ).fetchone()
                self.assertEqual(row[0], "revoked")
                self.assertEqual(row[1], 0)
                self.assertIsNotNone(row[2])

            with sqlite_connection(database) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT checksum FROM schema_migrations WHERE version=8"
                    ).fetchone()[0],
                    LEGACY_0008_SHA256,
                )


if __name__ == "__main__":
    unittest.main()
