from __future__ import annotations

import concurrent.futures
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from rag_ime.db import migration_runner
from rag_ime.team.errors import TeamError
from rag_ime.team.identity import TeamIdentityStore


class _Clock:
    def __init__(self, now_ms: int = 1_800_000_000_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


class TeamIdentityStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-team-identity-")
        self.db_path = Path(self.tmp.name) / "team.sqlite3"
        self.clock = _Clock()
        self.store = TeamIdentityStore(self.db_path, clock=self.clock)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _bootstrap(self) -> dict[str, object]:
        return self.store.bootstrap_admin("alice", "alice-password", "Alice")

    def test_bootstrap_login_and_database_do_not_expose_password_material(self) -> None:
        admin = self._bootstrap()
        self.assertEqual(admin["role"], "admin")
        self.assertNotIn("password", admin)
        self.assertNotIn("passwordHash", admin)

        login = self.store.login("alice", "alice-password")
        self.assertEqual(login["user"], admin)
        self.assertTrue(login["token"])
        self.assertTrue(login["csrfToken"])
        self.assertNotIn("passwordHash", login)

        authenticated = self.store.authenticate(str(login["token"]))
        self.assertEqual(authenticated["user"], admin)
        self.assertEqual(authenticated["csrfToken"], login["csrfToken"])
        self.assertEqual(authenticated["sessionId"], login["sessionId"])
        self.assertEqual(authenticated["expiresAtMs"], login["expiresAtMs"])
        self.store.require_csrf(str(login["token"]), str(login["csrfToken"]))
        self.store.logout(str(login["token"]))
        with self.assertRaises(TeamError):
            self.store.authenticate(str(login["token"]))

        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                users = conn.execute(
                    "SELECT password_hash FROM team_users WHERE username_key = 'alice'"
                ).fetchone()
                session = conn.execute(
                    "SELECT token_hash, csrf_hash FROM team_sessions"
                ).fetchone()
        self.assertIsNotNone(users)
        self.assertNotIn("alice-password", str(users[0]))
        hash_parts = str(users[0]).split("$")
        self.assertEqual(hash_parts[:4], ["scrypt", "16384", "8", "5"])
        self.assertNotEqual(str(session[0]), str(login["token"]))
        self.assertNotEqual(str(session[1]), str(login["csrfToken"]))

    def test_personal_spaces_are_isolated_and_forged_owner_does_not_bypass(self) -> None:
        admin = self._bootstrap()
        bob = self.store.create_member(str(admin["id"]), "bob", "bob-password")
        alice_spaces = self.store.list_spaces(str(admin["id"]))
        bob_spaces = self.store.list_spaces(str(bob["id"]))
        alice_personal = next(item for item in alice_spaces if item["kind"] == "personal")
        bob_personal = next(item for item in bob_spaces if item["kind"] == "personal")

        self.assertEqual(alice_personal["role"], "owner")
        with self.assertRaises(TeamError) as denied:
            self.store.require_space(str(bob["id"]), str(alice_personal["id"]))
        self.assertEqual((denied.exception.status, denied.exception.code), (404, "space_not_found"))

        with self.assertRaises(TeamError) as guessed:
            self.store.require_space(str(bob["id"]), "space_not-created-by-server")
        self.assertEqual(
            (guessed.exception.status, guessed.exception.code),
            (denied.exception.status, denied.exception.code),
        )
        with self.assertRaises(TeamError) as forged:
            self.store.require_space(str(bob["id"]), str(alice_personal["id"]), action="manage")
        self.assertEqual((forged.exception.status, forged.exception.code), (404, "space_not_found"))
        self.assertEqual(self.store.require_space(str(bob["id"]), str(bob_personal["id"]))["id"], bob_personal["id"])

    def test_project_roles_revoke_and_readd_advance_scope_revision(self) -> None:
        admin = self._bootstrap()
        bob = self.store.create_member(str(admin["id"]), "bob", "bob-password")
        carol = self.store.create_member(str(admin["id"]), "carol", "carol-password")
        outsider = self.store.create_member(str(admin["id"]), "outsider", "outsider-password")
        project = self.store.create_project(str(admin["id"]), "Shared project")
        project_id = str(project["id"])

        self.store.add_project_member(str(admin["id"]), project_id, str(bob["id"]), "viewer")
        with self.assertRaises(TeamError) as write_denied:
            self.store.require_space(str(bob["id"]), project_id, action="write")
        self.assertEqual((write_denied.exception.status, write_denied.exception.code), (403, "forbidden"))
        self.assertEqual(self.store.require_space(str(bob["id"]), project_id)["role"], "viewer")

        members = self.store.project_members(str(admin["id"]), project_id)
        self.assertEqual({item["role"] for item in members}, {"owner", "viewer"})
        initial_revision = int(self.store.require_space(str(admin["id"]), project_id)["revision"])
        removed = self.store.remove_project_member(str(admin["id"]), project_id, str(bob["id"]))
        self.assertTrue(removed["removed"])
        self.assertGreater(int(removed["revision"]), initial_revision)
        removed_membership_revision = int(removed["membershipRevision"])
        with closing(sqlite3.connect(self.db_path)) as conn:
            tombstone = conn.execute(
                "SELECT active, membership_revision FROM team_project_members WHERE space_id = ? AND user_id = ?",
                (project_id, str(bob["id"])),
            ).fetchone()
        self.assertEqual(tuple(tombstone), (0, removed_membership_revision))
        with self.assertRaisesRegex(TeamError, "space not found"):
            self.store.require_space(str(bob["id"]), project_id)

        readded = self.store.add_project_member(
            str(admin["id"]), project_id, str(bob["id"]), "contributor"
        )
        self.assertEqual(readded["role"], "contributor")
        self.assertGreater(int(readded["revision"]), int(removed["revision"]))
        self.assertGreater(int(readded["membershipRevision"]), removed_membership_revision)
        self.assertEqual(self.store.require_space(str(bob["id"]), project_id, action="write")["role"], "contributor")
        bob_membership_revision = int(
            self.store.require_space(str(bob["id"]), project_id)["membershipRevision"]
        )
        contributor_members = self.store.project_members(str(bob["id"]), project_id)
        self.assertIn(str(bob["id"]), {str(item["id"]) for item in contributor_members})
        self.assertTrue(
            all("password" not in item and "passwordHash" not in item for item in contributor_members)
        )
        with self.assertRaises(TeamError) as outsider_denied:
            self.store.project_members(str(outsider["id"]), project_id)
        self.assertEqual((outsider_denied.exception.status, outsider_denied.exception.code), (404, "space_not_found"))

        self.store.add_project_member(str(admin["id"]), project_id, str(carol["id"]), "maintainer")
        self.assertEqual(
            self.store.require_space(str(bob["id"]), project_id)["membershipRevision"],
            bob_membership_revision,
        )
        self.assertEqual(
            {item["id"] for item in self.store.project_members(str(bob["id"]), project_id)},
            {admin["id"], bob["id"], carol["id"]},
        )

    def test_disabled_account_invalidates_existing_session_and_membership_checks(self) -> None:
        admin = self._bootstrap()
        bob = self.store.create_member(str(admin["id"]), "bob", "bob-password")
        login = self.store.login("bob", "bob-password")
        self.assertEqual(self.store.authenticate(str(login["token"]))["user"]["id"], bob["id"])

        self.store.set_member_active(str(admin["id"]), str(bob["id"]), False)
        with self.assertRaises(TeamError) as auth_error:
            self.store.authenticate(str(login["token"]))
        self.assertEqual((auth_error.exception.status, auth_error.exception.code), (401, "authentication_required"))
        with self.assertRaises(TeamError) as space_error:
            self.store.list_spaces(str(bob["id"]))
        self.assertEqual((space_error.exception.status, space_error.exception.code), (401, "account_disabled"))

    def test_active_member_directory_is_readable_by_project_owner_without_credentials(self) -> None:
        admin = self._bootstrap()
        owner = self.store.create_member(str(admin["id"]), "owner", "owner-password", "Project Owner")
        disabled = self.store.create_member(str(admin["id"]), "disabled", "disabled-password")
        self.store.set_member_active(str(admin["id"]), str(disabled["id"]), False)
        project = self.store.create_project(str(owner["id"]), "Owner project")

        with self.assertRaises(TeamError) as admin_only:
            self.store.list_members(str(owner["id"]))
        self.assertEqual((admin_only.exception.status, admin_only.exception.code), (403, "forbidden"))

        directory = self.store.member_directory(str(owner["id"]))
        self.assertEqual(
            {entry["id"] for entry in directory},
            {str(admin["id"]), str(owner["id"])},
        )
        owner_entry = next(entry for entry in directory if entry["id"] == owner["id"])
        self.assertEqual(
            owner_entry,
            {"id": owner["id"], "username": "owner", "displayName": "Project Owner"},
        )
        self.assertTrue(
            all(
                set(entry) == {"id", "username", "displayName"}
                and "password" not in entry
                and "passwordHash" not in entry
                and "token" not in entry
                and "csrfToken" not in entry
                for entry in directory
            )
        )
        with self.assertRaises(TeamError) as disabled_actor:
            self.store.member_directory(str(disabled["id"]))
        self.assertEqual((disabled_actor.exception.status, disabled_actor.exception.code), (401, "account_disabled"))
        self.assertEqual(project["kind"], "project")

    def test_last_active_admin_and_last_project_manager_are_protected(self) -> None:
        admin = self._bootstrap()
        second_admin = self.store.create_member(
            str(admin["id"]), "second-admin", "second-password", role="admin"
        )
        self.store.set_member_active(str(admin["id"]), str(admin["id"]), False)
        self.assertFalse(self.store.list_members(str(second_admin["id"]))[0]["active"])
        with self.assertRaises(TeamError) as last_admin:
            self.store.set_member_active(str(second_admin["id"]), str(second_admin["id"]), False)
        self.assertEqual((last_admin.exception.status, last_admin.exception.code), (409, "last_active_admin"))

        project = self.store.create_project(str(second_admin["id"]), "Protected")
        project_id = str(project["id"])
        maintainer = self.store.create_member(
            str(second_admin["id"]), "maintainer", "maintainer-password"
        )
        self.store.add_project_member(
            str(second_admin["id"]), project_id, str(maintainer["id"]), "maintainer"
        )
        with self.assertRaises(TeamError) as owner_remove:
            self.store.remove_project_member(str(second_admin["id"]), project_id, str(second_admin["id"]))
        self.assertEqual((owner_remove.exception.status, owner_remove.exception.code), (409, "last_manager"))
        self.store.remove_project_member(str(second_admin["id"]), project_id, str(maintainer["id"]))
        with self.assertRaises(TeamError) as owner_remove_again:
            self.store.remove_project_member(str(second_admin["id"]), project_id, str(second_admin["id"]))
        self.assertEqual((owner_remove_again.exception.status, owner_remove_again.exception.code), (409, "last_manager"))

    def test_concurrent_bootstrap_only_has_one_winner_and_usernames_are_unique(self) -> None:
        def bootstrap() -> tuple[str, str]:
            store = TeamIdentityStore(self.db_path, clock=self.clock)
            try:
                user = store.bootstrap_admin("race", "race-password")
                return ("ok", str(user["id"]))
            except TeamError as error:
                return ("error", error.code)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _item: bootstrap(), range(2)))
        self.assertEqual(sum(result[0] == "ok" for result in results), 1)
        self.assertEqual(sum(result == ("error", "bootstrap_already_complete") for result in results), 1)

        winner_id = next(result[1] for result in results if result[0] == "ok")

        def create_duplicate() -> tuple[str, str]:
            store = TeamIdentityStore(self.db_path, clock=self.clock)
            try:
                user = store.create_member(winner_id, "duplicate", "another-password")
                return ("ok", str(user["id"]))
            except TeamError as error:
                return ("error", error.code)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            duplicate_results = list(executor.map(lambda _item: create_duplicate(), range(2)))
        self.assertEqual(sum(result[0] == "ok" for result in duplicate_results), 1)
        self.assertEqual(
            sum(result == ("error", "username_taken") for result in duplicate_results),
            1,
        )

        with self.assertRaises(TeamError) as duplicate:
            self.store.create_member(winner_id, "RACE", "another-password")
        self.assertEqual((duplicate.exception.status, duplicate.exception.code), (409, "username_taken"))

    def test_concurrent_initialization_applies_team_migrations_atomically(self) -> None:
        start = threading.Barrier(2)
        first_migration_paused = threading.Event()
        second_migration_reached = threading.Event()
        release_first_migration = threading.Event()
        marker_lock = threading.Lock()
        marker_calls = 0
        original_execute = migration_runner._execute_sql_script

        def pause_between_team_migrations(
            conn: sqlite3.Connection,
            sql: str,
        ) -> None:
            nonlocal marker_calls
            if "CREATE TABLE team_session_bindings" not in sql:
                original_execute(conn, sql)
                return
            with marker_lock:
                marker_calls += 1
                call_number = marker_calls
            if call_number == 1:
                first_migration_paused.set()
                if not release_first_migration.wait(timeout=5):
                    raise AssertionError("migration race test did not release the first runner")
            else:
                second_migration_reached.set()
            original_execute(conn, sql)

        def initialize(_item: int) -> tuple[str, int | str]:
            try:
                start.wait(timeout=5)
                return ("ok", TeamIdentityStore(self.db_path, clock=self.clock).initialize())
            except Exception as error:
                return ("error", f"{type(error).__name__}: {error}")

        with mock.patch.object(
            migration_runner,
            "_execute_sql_script",
            pause_between_team_migrations,
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(initialize, item) for item in range(2)]
                self.assertTrue(first_migration_paused.wait(timeout=5))
                # The unfixed runner releases its writer lock after migration
                # 0001, so the second initializer can reach 0002 while the
                # first is paused.  The fixed runner keeps that initializer
                # blocked until all team migrations finish.  Give the old
                # interleaving a bounded scheduling window, then release both
                # implementations.
                second_migration_reached.wait(timeout=0.1)
                time.sleep(0.05)
                release_first_migration.set()
                results = [future.result() for future in futures]

        self.assertEqual(results, [("ok", 11)] * 2)

    def test_isolated_migration_directory_does_not_inherit_default_hooks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="paw-team-migrations-") as directory:
            migrations_dir = Path(directory)
            (migrations_dir / "0001_team_probe.sql").write_text(
                "CREATE TABLE team_probe (id INTEGER PRIMARY KEY);\n",
                encoding="utf-8",
            )
            wrong_hook = mock.Mock()
            with mock.patch.object(migration_runner, "_MIGRATION_HOOKS", {1: wrong_hook}):
                with closing(sqlite3.connect(self.db_path)) as conn:
                    result = migration_runner.apply_database_migrations(
                        conn,
                        migrations_dir=migrations_dir,
                    )
                    exists = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'team_probe'"
                    ).fetchone()
            self.assertEqual(result.current_version, 1)
            self.assertIsNotNone(exists)
            wrong_hook.assert_not_called()

    def test_login_lockout_is_bounded_and_expiry_is_enforced(self) -> None:
        self._bootstrap()
        for _ in range(5):
            with self.assertRaises(TeamError) as invalid:
                self.store.login("alice", "wrong-password")
            self.assertEqual((invalid.exception.status, invalid.exception.code), (401, "invalid_credentials"))
        with self.assertRaises(TeamError) as locked:
            self.store.login("alice", "alice-password")
        self.assertEqual((locked.exception.status, locked.exception.code), (401, "invalid_credentials"))

        self.clock.now_ms += 30_001
        login = self.store.login("alice", "alice-password")
        self.clock.now_ms = int(login["session"]["expiresAtMs"]) + 1
        with self.assertRaises(TeamError) as expired:
            self.store.authenticate(str(login["token"]))
        self.assertEqual((expired.exception.status, expired.exception.code), (401, "authentication_required"))


if __name__ == "__main__":
    unittest.main()
