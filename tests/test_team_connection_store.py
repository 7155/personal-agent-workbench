from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from rag_ime.team.connection_store import TeamConnectionStore
from rag_ime.team.errors import TeamError
from rag_ime.team.grants import TeamGrantStore
from rag_ime.team.identity import TeamIdentityStore
from rag_ime.team.secret_vault import TeamSecretVault


class _Clock:
    def __init__(self, now_ms: int = 1_800_000_000_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


class TeamConnectionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-team-connections-")
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "team.sqlite3"
        self.clock = _Clock()
        self.identity = TeamIdentityStore(self.db_path, clock=self.clock)
        self.admin = self.identity.bootstrap_admin("admin", "administrator-password", "Admin")
        self.alice = self.identity.create_member(
            self.admin["id"], "alice", "alice-password-123", "Alice"
        )
        self.bob = self.identity.create_member(
            self.admin["id"], "bob", "bob-password-123", "Bob"
        )
        self.carol = self.identity.create_member(
            self.admin["id"], "carol", "carol-password-123", "Carol"
        )
        self.project = self.identity.create_project(self.admin["id"], "Shared site")
        self.project_id = str(self.project["id"])
        self.identity.add_project_member(
            self.admin["id"], self.project_id, self.alice["id"], "maintainer"
        )
        self.identity.add_project_member(
            self.admin["id"], self.project_id, self.bob["id"], "contributor"
        )
        self.identity.add_project_member(
            self.admin["id"], self.project_id, self.carol["id"], "viewer"
        )
        self.personal_space_id = self._personal_space(self.admin)
        self.admin_project_workspace = self.root / "admin-project-workspace"
        self.admin_project_workspace.mkdir()
        self.admin_personal_workspace = self.root / "admin-personal-workspace"
        self.admin_personal_workspace.mkdir()
        self.alice_workspace = self.root / "alice-workspace"
        self.alice_workspace.mkdir()
        self.bob_workspace = self.root / "bob-workspace"
        self.bob_workspace.mkdir()
        self.grants = TeamGrantStore(self.db_path, self.identity, now_ms=self.clock)
        self.grants.bind_session(
            str(self.admin["id"]),
            self.personal_space_id,
            "agent:admin-personal",
            self.admin_personal_workspace,
        )
        self.grants.bind_session(
            str(self.admin["id"]), self.project_id, "agent:admin", self.admin_project_workspace
        )
        self.grants.bind_session(
            str(self.alice["id"]), self.project_id, "agent:alice", self.alice_workspace
        )
        self.grants.bind_session(
            str(self.bob["id"]), self.project_id, "agent:bob", self.bob_workspace
        )
        self.vault_key_path = self.root / "vault" / "key"
        self.store = TeamConnectionStore(
            self.identity,
            self.grants,
            self.vault_key_path,
            now_ms=self.clock,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _personal_space(self, user: dict[str, object]) -> str:
        spaces = self.identity.list_spaces(str(user["id"]))
        return str(next(space["id"] for space in spaces if space["kind"] == "personal"))

    def _personal_connection(self) -> dict[str, object]:
        return self.store.create_connection(
            str(self.admin["id"]),
            self.personal_space_id,
            scope="personal",
            label="Admin GitHub",
            repositories=["Acme/App"],
            operations=["repo.read", "issue.read"],
            account_login="admin-gh",
            credentials={"accessToken": "personal-secret-token", "tokenType": "bearer"},
        )

    def _project_connection(self) -> dict[str, object]:
        return self.store.create_connection(
            str(self.alice["id"]),
            self.project_id,
            scope="project",
            label="Shared GitHub",
            repositories=["Acme/App"],
            operations=["repo.read", "issue.read", "issue.create"],
            account_login="shared-gh",
            credentials={"accessToken": "project-secret-token", "tokenType": "bearer"},
        )

    def test_public_metadata_has_no_credentials_and_personal_scope_is_private(self) -> None:
        connection = self._personal_connection()
        self.assertEqual(
            set(connection),
            {
                "id",
                "provider",
                "scope",
                "ownerId",
                "label",
                "accountLogin",
                "repositories",
                "operations",
                "status",
                "revision",
                "canManage",
                "createdAtMs",
            },
        )
        self.assertEqual(connection["scope"], "personal")
        self.assertNotIn("credentials", connection)
        self.assertNotIn("accessToken", connection)
        self.assertTrue(connection["canManage"])
        self.assertEqual(
            self.store.list_connections(str(self.admin["id"]), self.personal_space_id),
            [connection],
        )
        with self.assertRaises(TeamError) as error:
            self.store.list_connections(str(self.bob["id"]), self.personal_space_id)
        self.assertEqual((error.exception.status, error.exception.code), (404, "space_not_found"))

    def test_project_connection_survives_creator_leaving_and_platform_admin_has_no_bypass(self) -> None:
        connection = self._project_connection()
        self.identity.remove_project_member(
            self.admin["id"], self.project_id, self.alice["id"]
        )
        listed = self.store.list_connections(str(self.bob["id"]), self.project_id)
        self.assertEqual(listed[0]["id"], connection["id"])
        self.assertEqual(listed[0]["ownerId"], self.project_id)
        self.assertFalse(listed[0]["canManage"])
        with self.assertRaises(TeamError):
            self.store.revoke_connection(str(self.alice["id"]), self.project_id, str(connection["id"]))

        other_admin = self.identity.create_member(
            self.admin["id"], "other-admin", "other-admin-password", role="admin"
        )
        with self.assertRaises(TeamError) as error:
            self.store.list_connections(str(other_admin["id"]), self.project_id)
        self.assertEqual((error.exception.status, error.exception.code), (404, "space_not_found"))
        with self.assertRaises(TeamError):
            self.store.create_connection(
                str(other_admin["id"]),
                self.project_id,
                scope="project",
                label="bypass",
                repositories=["acme/app"],
                operations=["repo.read"],
                account_login="bypass",
                credentials={"accessToken": "never"},
            )

    def test_allowlist_and_scope_validation_are_bounded(self) -> None:
        with self.assertRaises(TeamError):
            self.store.create_connection(
                str(self.bob["id"]),
                self.project_id,
                scope="project",
                label="viewer cannot create",
                repositories=["acme/app"],
                operations=["repo.read"],
                account_login="bob",
                credentials={"accessToken": "secret"},
            )
        with self.assertRaises(TeamError):
            self.store.create_connection(
                str(self.alice["id"]),
                self.project_id,
                scope="project",
                label="bad repository",
                repositories=["https://github.com/acme/app"],
                operations=["repo.read"],
                account_login="alice",
                credentials={"accessToken": "secret"},
            )
        with self.assertRaises(TeamError):
            self.store.create_connection(
                str(self.alice["id"]),
                self.project_id,
                scope="project",
                label="bad operation",
                repositories=["acme/app"],
                operations=["repo.write"],
                account_login="alice",
                credentials={"accessToken": "secret"},
            )

    def test_grant_is_for_actor_owned_session_and_is_not_inherited_across_sessions_or_attempts(self) -> None:
        connection = self._personal_connection()
        grant = self.store.create_grant(
            str(self.admin["id"]),
            self.personal_space_id,
            connection_id=str(connection["id"]),
            session_id="agent:admin-personal",
            repository="acme/app",
            operations=["repo.read"],
        )
        first = self.grants.issue_attempt("agent:admin-personal")
        second = self.grants.issue_attempt("agent:admin-personal")
        self.assertNotEqual(first["generation"], second["generation"])
        authorized = self.store.authorize(
            "agent:admin-personal", str(grant["id"]), "repo.read", "ACME/APP"
        )
        self.assertEqual(authorized["grant"]["id"], grant["id"])
        self.assertEqual(authorized["credentials"]["accessToken"], "personal-secret-token")

        child_workspace = self.root / "admin-child-workspace"
        child_workspace.mkdir()
        self.grants.bind_session(
            str(self.admin["id"]), self.personal_space_id, "agent:admin-child", child_workspace
        )
        self.assertEqual(self.store.task_grants("agent:admin-child"), [])
        with self.assertRaises(TeamError):
            self.store.create_grant(
                str(self.bob["id"]),
                self.personal_space_id,
                connection_id=str(connection["id"]),
                session_id="agent:admin-personal",
                repository="acme/app",
                operations=["repo.read"],
            )

    def test_project_contributor_can_grant_but_revocation_and_rejoin_fence_use(self) -> None:
        connection = self._project_connection()
        grant = self.store.create_grant(
            str(self.bob["id"]),
            self.project_id,
            connection_id=str(connection["id"]),
            session_id="agent:bob",
            repository="acme/app",
            operations=["repo.read"],
        )
        self.assertEqual(self.store.task_grants("agent:bob")[0]["id"], grant["id"])
        self.store.revoke_grant(str(self.bob["id"]), self.project_id, str(grant["id"]))
        with self.assertRaises(TeamError):
            self.store.authorize("agent:bob", str(grant["id"]), "repo.read", "acme/app")

        replacement = self.store.create_grant(
            str(self.bob["id"]),
            self.project_id,
            connection_id=str(connection["id"]),
            session_id="agent:bob",
            repository="acme/app",
            operations=["repo.read"],
        )
        self.identity.remove_project_member(
            self.admin["id"], self.project_id, self.bob["id"]
        )
        self.identity.add_project_member(
            self.admin["id"], self.project_id, self.bob["id"], "contributor"
        )
        with self.assertRaises(TeamError):
            self.store.authorize("agent:bob", str(replacement["id"]), "repo.read", "acme/app")
        listed = self.store.list_grants(
            str(self.bob["id"]), self.project_id, session_id="agent:bob"
        )
        self.assertEqual(next(item for item in listed if item["id"] == replacement["id"])["status"], "revoked")

    def test_personal_disable_reenable_does_not_restore_old_binding(self) -> None:
        alice_personal = self._personal_space(self.alice)
        workspace = self.root / "alice-personal-workspace"
        workspace.mkdir()
        self.grants.bind_session(
            str(self.alice["id"]), alice_personal, "agent:alice-personal", workspace
        )
        connection = self.store.create_connection(
            str(self.alice["id"]),
            alice_personal,
            scope="personal",
            label="Alice personal",
            repositories=["acme/app"],
            operations=["repo.read"],
            account_login="alice-gh",
            credentials={"accessToken": "alice-secret"},
        )
        grant = self.store.create_grant(
            str(self.alice["id"]),
            alice_personal,
            connection_id=str(connection["id"]),
            session_id="agent:alice-personal",
            repository="acme/app",
            operations=["repo.read"],
        )
        self.identity.set_member_active(self.admin["id"], self.alice["id"], False)
        self.identity.set_member_active(self.admin["id"], self.alice["id"], True)
        with self.assertRaises(TeamError):
            self.store.authorize(
                "agent:alice-personal", str(grant["id"]), "repo.read", "acme/app"
            )
        with self.assertRaises(TeamError):
            self.store.create_grant(
                str(self.alice["id"]),
                alice_personal,
                connection_id=str(connection["id"]),
                session_id="agent:alice-personal",
                repository="acme/app",
                operations=["repo.read"],
            )

    def test_expiry_and_connection_revoke_remove_usable_secret(self) -> None:
        connection = self._personal_connection()
        grant = self.store.create_grant(
            str(self.admin["id"]),
            self.personal_space_id,
            connection_id=str(connection["id"]),
            session_id="agent:admin-personal",
            repository="acme/app",
            operations=["repo.read"],
            ttl_seconds=60,
        )
        self.clock.now_ms += 60_001
        self.assertEqual(self.store.task_grants("agent:admin-personal"), [])
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            status = conn.execute(
                "SELECT status FROM team_connection_grants WHERE id = ?", (grant["id"],)
            ).fetchone()[0]
        self.assertEqual(status, "expired")

        revoked = self.store.revoke_connection(
            str(self.admin["id"]), self.personal_space_id, str(connection["id"])
        )
        self.assertEqual(revoked["status"], "revoked")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            secret_blob = conn.execute(
                "SELECT secret_blob FROM team_connections WHERE id = ?", (connection["id"],)
            ).fetchone()[0]
        self.assertIsNone(secret_blob)
        with self.assertRaises(TeamError):
            self.store.authorize("agent:admin-personal", str(grant["id"]), "repo.read", "acme/app")
        self.assertNotIn(b"personal-secret-token", self.db_path.read_bytes())

    def test_tamper_is_rejected_and_successful_refresh_keeps_grant_revision(self) -> None:
        connection = self._personal_connection()
        grant = self.store.create_grant(
            str(self.admin["id"]),
            self.personal_space_id,
            connection_id=str(connection["id"]),
            session_id="agent:admin-personal",
            repository="acme/app",
            operations=["repo.read"],
        )
        old_revision = int(connection["revision"])
        old_secret = self.store.begin_refresh(str(connection["id"]))
        self.assertEqual(old_secret["accessToken"], "personal-secret-token")
        with self.assertRaises(TeamError):
            self.store.begin_refresh(str(connection["id"]))
        self.store.finish_refresh(
            str(connection["id"]),
            {"accessToken": "rotated-secret-token", "tokenType": "bearer"},
        )
        refreshed = self.store.list_connections(str(self.admin["id"]), self.personal_space_id)[0]
        self.assertEqual(refreshed["revision"], old_revision)
        self.assertEqual(
            self.store.authorize("agent:admin-personal", str(grant["id"]), "repo.read", "acme/app")[
                "credentials"
            ]["accessToken"],
            "rotated-secret-token",
        )

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE team_connections SET secret_blob = ? WHERE id = ?",
                (b"tampered", connection["id"]),
            )
        with self.assertRaises(TeamError) as error:
            self.store.read_credentials(str(connection["id"]))
        self.assertEqual(error.exception.code, "connection_secret_invalid")

    def test_failed_refresh_blocks_use_and_revoke_wins_over_finish(self) -> None:
        failed = self._personal_connection()
        self.store.begin_refresh(str(failed["id"]))
        self.store.fail_refresh(str(failed["id"]))
        with self.assertRaises(TeamError):
            self.store.read_credentials(str(failed["id"]))

        raced = self.store.create_connection(
            str(self.admin["id"]),
            self.personal_space_id,
            scope="personal",
            label="Raced refresh",
            repositories=["acme/app"],
            operations=["repo.read"],
            account_login="admin-gh",
            credentials={"accessToken": "race-token"},
        )
        self.store.begin_refresh(str(raced["id"]))
        self.store.revoke_connection(
            str(self.admin["id"]), self.personal_space_id, str(raced["id"])
        )
        with self.assertRaises(TeamError):
            self.store.finish_refresh(str(raced["id"]), {"accessToken": "must-not-reactivate"})
        raced_status = next(
            item["status"]
            for item in self.store.list_connections(str(self.admin["id"]), self.personal_space_id)
            if item["id"] == raced["id"]
        )
        self.assertEqual(raced_status, "revoked")

    def test_cancel_refresh_preserves_project_credential_and_peer_grants(self) -> None:
        connection = self._project_connection()
        alice_grant = self.store.create_grant(
            str(self.alice["id"]),
            self.project_id,
            connection_id=str(connection["id"]),
            session_id="agent:alice",
            repository="acme/app",
            operations=["repo.read"],
        )
        bob_grant = self.store.create_grant(
            str(self.bob["id"]),
            self.project_id,
            connection_id=str(connection["id"]),
            session_id="agent:bob",
            repository="acme/app",
            operations=["repo.read"],
        )
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            before = conn.execute(
                "SELECT revision, secret_version, secret_blob FROM team_connections WHERE id = ?",
                (connection["id"],),
            ).fetchone()
        self.store.begin_refresh(str(connection["id"]))
        # The initiating task can lose its grant during the pre-dispatch
        # authorization check.  Cancelling that refresh must not tear down a
        # project credential still granted to another member.
        self.store.revoke_grant(
            str(self.alice["id"]), self.project_id, str(alice_grant["id"])
        )
        self.store.cancel_refresh(str(connection["id"]))
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            after = conn.execute(
                "SELECT revision, secret_version, secret_blob, status, refresh_state "
                "FROM team_connections WHERE id = ?",
                (connection["id"],),
            ).fetchone()
        self.assertEqual(after[:3], before)
        self.assertEqual(after[3:], ("active", "idle"))
        self.assertEqual(
            self.store.authorize("agent:bob", str(bob_grant["id"]), "repo.read", "acme/app")[
                "credentials"
            ]["accessToken"],
            "project-secret-token",
        )
        with self.assertRaises(TeamError):
            self.store.authorize("agent:alice", str(alice_grant["id"]), "repo.read", "acme/app")

    def test_creation_can_be_bound_to_current_login_session_only(self) -> None:
        login = self.identity.login("admin", "administrator-password")
        self.store.create_connection(
            str(self.admin["id"]),
            self.personal_space_id,
            scope="personal",
            label="Bound login",
            repositories=["acme/app"],
            operations=["repo.read"],
            account_login="admin-gh",
            credentials={"accessToken": "login-bound-token"},
            login_session_id=str(login["sessionId"]),
        )
        self.identity.logout(str(login["token"]))
        with self.assertRaises(TeamError) as error:
            self.store.create_connection(
                str(self.admin["id"]),
                self.personal_space_id,
                scope="personal",
                label="Stale login",
                repositories=["acme/app"],
                operations=["repo.read"],
                account_login="admin-gh",
                credentials={"accessToken": "stale-login-token"},
                login_session_id=str(login["sessionId"]),
            )
        self.assertEqual(error.exception.status, 401)

    def test_vault_key_is_owner_only_and_aad_binds_connection_identity(self) -> None:
        vault = TeamSecretVault(self.root / "standalone-vault" / "key")
        sealed = vault.seal(
            {"accessToken": "secret"},
            owner_id="usr-one",
            connection_id="conn-one",
            provider="github",
            version=1,
        )
        self.assertEqual(
            vault.open(
                sealed,
                owner_id="usr-one",
                connection_id="conn-one",
                provider="github",
                version=1,
            )["accessToken"],
            "secret",
        )
        with self.assertRaises(TeamError):
            vault.open(
                sealed,
                owner_id="usr-two",
                connection_id="conn-one",
                provider="github",
                version=1,
            )
        self.assertEqual((self.root / "standalone-vault" / "key").stat().st_mode & 0o077, 0)

    def test_vault_rejects_key_and_parent_symlinks(self) -> None:
        target = self.root / "target-key"
        target.write_bytes(b"x" * 32)
        target.chmod(0o600)
        key_link = self.root / "key-link"
        key_link.symlink_to(target)
        with self.assertRaises(TeamError):
            TeamSecretVault(key_link)

        real_parent = self.root / "real-parent"
        real_parent.mkdir(mode=0o700)
        parent_link = self.root / "parent-link"
        parent_link.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaises(TeamError):
            TeamSecretVault(parent_link / "key")

    def test_existing_ciphertext_never_triggers_silent_new_key_creation(self) -> None:
        connection = self._personal_connection()
        self.assertTrue(connection["id"])
        self.vault_key_path.unlink()
        with self.assertRaises(TeamError) as error:
            TeamConnectionStore(self.identity, self.grants, self.vault_key_path, now_ms=self.clock)
        self.assertEqual(error.exception.code, "connection_vault_key_missing")


if __name__ == "__main__":
    unittest.main()
