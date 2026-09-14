from __future__ import annotations

import concurrent.futures
from pathlib import Path
import sqlite3
import tempfile
import unittest

from rag_ime.db import sqlite_connection
from rag_ime.team.errors import TeamError
from rag_ime.team.identity import TeamIdentityStore
from rag_ime.team.preview_store import TeamPreviewStore


class _Clock:
    def __init__(self) -> None:
        self.now_ms = 1_800_000_000_000

    def __call__(self) -> int:
        return self.now_ms


class TeamPreviewStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-team-preview-")
        self.root = Path(self.tmp.name)
        self.clock = _Clock()
        self.identity = TeamIdentityStore(self.root / "team.sqlite3", clock=self.clock)
        self.admin = self.identity.bootstrap_admin(
            "admin",
            "admin-password-123",
            display_name="Project owner",
        )
        self.alice = self.identity.create_member(
            str(self.admin["id"]),
            "alice",
            "alice-password-123",
            display_name="Alice",
        )
        self.bob = self.identity.create_member(
            str(self.admin["id"]),
            "bob",
            "bob-password-123",
            display_name="Bob",
        )
        self.viewer = self.identity.create_member(
            str(self.admin["id"]),
            "viewer",
            "viewer-password-123",
            display_name="Read only",
        )
        self.project = self.identity.create_project(str(self.admin["id"]), "Shared site")
        self.project_id = str(self.project["id"])
        self.identity.add_project_member(
            str(self.admin["id"]), self.project_id, str(self.alice["id"]), "maintainer"
        )
        self.identity.add_project_member(
            str(self.admin["id"]), self.project_id, str(self.bob["id"]), "contributor"
        )
        self.identity.add_project_member(
            str(self.admin["id"]), self.project_id, str(self.viewer["id"]), "viewer"
        )
        self._insert_project(self.project_id, "main", "a" * 40)
        self.store = TeamPreviewStore(self.identity, clock=self.clock)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _insert_project(self, space_id: str, branch: str, head_commit: str) -> None:
        now = self.clock.now_ms
        with sqlite_connection(self.identity.db_path, foreign_keys=True) as conn:
            conn.execute(
                """
                INSERT INTO team_projects(
                    space_id, target_branch, repository_path, head_commit,
                    revision, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (space_id, branch, str(self.root / "trusted-repository"), head_commit, now, now),
            )

    def _set_head(self, commit: str, branch: str = "main") -> None:
        with sqlite_connection(self.identity.db_path, foreign_keys=True) as conn:
            conn.execute(
                """
                UPDATE team_projects
                SET head_commit = ?, revision = revision + 1, updated_at_ms = ?
                WHERE space_id = ? AND target_branch = ?
                """,
                (commit, self.clock.now_ms, self.project_id, branch),
            )

    def _publish_brief(self, revision: int, objective: str = "Ship preview") -> None:
        with sqlite_connection(self.identity.db_path, foreign_keys=True) as conn:
            conn.execute(
                """
                INSERT INTO team_project_briefs(
                    space_id, revision, objective, acceptance_criteria_json,
                    updated_at_ms, updated_by_user_id, updated_by_display_name
                ) VALUES (?, ?, ?, '[]', ?, ?, 'Project owner')
                """,
                (self.project_id, revision, objective, self.clock.now_ms, self.admin["id"]),
            )

    def _reserve(
        self,
        actor: dict[str, object] | None = None,
        *,
        commit: str = "a" * 40,
        requirements_revision: int = 0,
        request_id: str = "request-1",
    ) -> dict[str, object]:
        return self.store.reserve(
            str((actor or self.admin)["id"]),
            self.project_id,
            branch="main",
            commit=commit,
            requirements_revision=requirements_revision,
            client_request_id=request_id,
        )

    def _login(self, user: dict[str, object], password: str) -> dict[str, object]:
        return self.identity.login(str(user["username"]), password)

    def _assert_error(
        self,
        callback,
        *,
        status: int,
        code: str,
    ) -> TeamError:
        with self.assertRaises(TeamError) as caught:
            callback()
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code))
        return caught.exception

    def test_reserve_is_idempotent_and_serializes_one_starting_preview(self) -> None:
        def reserve_once(_index: int) -> tuple[str, object]:
            try:
                return "ok", self._reserve(request_id="same-client-request")
            except TeamError as error:
                return "error", error

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reserve_once, (1, 2)))
        successes = [value for kind, value in results if kind == "ok"]
        self.assertEqual(len(successes), 2)
        self.assertEqual({str(value["id"]) for value in successes}, {str(successes[0]["id"])})

        self._assert_error(
            lambda: self._reserve(request_id="different-request"),
            status=409,
            code="preview_starting_exists",
        )
        self.assertEqual(
            self.store.snapshot(str(successes[0]["id"]))["requestedByDisplayName"],
            "Project owner",
        )

    def test_activation_checks_latest_request_membership_brief_and_stable_head(self) -> None:
        candidate = self._reserve()
        self._publish_brief(1)
        self._assert_error(
            lambda: self.store.activate(str(self.admin["id"]), str(candidate["id"])),
            status=409,
            code="preview_stale",
        )
        self.assertIsNone(self.store.public_status(str(self.admin["id"]), self.project_id)["active"])
        self.store.mark_stopped(str(candidate["id"]), "candidate discarded")

        self._set_head("b" * 40)
        stale_commit = self._reserve(
            commit="a" * 40,
            requirements_revision=1,
            request_id="stale-head",
        )
        self._assert_error(
            lambda: self.store.activate(str(self.admin["id"]), str(stale_commit["id"])),
            status=409,
            code="preview_stale",
        )
        self.store.mark_stopped(str(stale_commit["id"]), "stale commit")

        self._set_head("a" * 40)
        valid = self._reserve(requirements_revision=1, request_id="valid")
        self.assertEqual(
            self.store.activate(str(self.alice["id"]), str(valid["id"]))["status"],
            "ready",
        )
        self.assertEqual(
            self.store.public_status(str(self.alice["id"]), self.project_id)["active"]["id"],
            valid["id"],
        )

    def test_requester_revocation_blocks_activation_but_does_not_remove_existing_preview(self) -> None:
        first = self._reserve()
        self.store.activate(str(self.admin["id"]), str(first["id"]))
        second = self._reserve(actor=self.alice, request_id="alice-request")
        self.identity.remove_project_member(
            str(self.admin["id"]), self.project_id, str(self.alice["id"])
        )
        self._assert_error(
            lambda: self.store.activate(str(self.admin["id"]), str(second["id"])),
            status=409,
            code="preview_request_revoked",
        )
        status = self.store.public_status(str(self.admin["id"]), self.project_id)
        self.assertEqual(status["active"]["id"], first["id"])
        self.assertEqual(status["active"]["status"], "ready")

    def test_preview_start_is_a_project_management_capability(self) -> None:
        for member in (self.bob, self.viewer):
            with self.subTest(role=member['username']):
                self._assert_error(lambda actor=member: self._reserve(actor=actor), status=403, code='forbidden')
        self.assertFalse(self.store.list_live())

    def test_retained_previews_are_recoverable_and_recovery_revokes_leases(self) -> None:
        first = self._reserve()
        self.store.activate(str(self.admin["id"]), str(first["id"]))
        login = self._login(self.admin, "admin-password-123")
        ticket = self.store.issue_ticket(
            str(self.admin["id"]), self.project_id, str(login["sessionId"])
        )
        lease = self.store.consume_ticket(str(first["id"]), str(ticket["ticket"]))

        self._set_head("b" * 40)
        second = self._reserve(commit="b" * 40, request_id="second")
        self.store.activate(str(self.admin["id"]), str(second["id"]))
        live_ids = {str(item["id"]) for item in self.store.list_live()}
        self.assertIn(str(first["id"]), live_ids)
        self.assertIn(str(second["id"]), live_ids)
        self.assertEqual(self.store.snapshot(str(first["id"]))["status"], "retained")
        self.assertEqual(
            self.store.authorize_lease(str(first["id"]), str(lease["leaseToken"]))["role"],
            "owner",
        )

        quarantined = self.store.mark_recovery_required(str(first["id"]), "controller lost")
        self.assertEqual(quarantined["status"], "recovery_required")
        self._assert_error(
            lambda: self.store.authorize_lease(str(first["id"]), str(lease["leaseToken"])),
            status=401,
            code="preview_lease_invalid",
        )
        self.assertNotIn(
            str(first["id"]),
            {str(item["id"]) for item in self.store.list_live() if item["status"] == "stopped"},
        )
        stopped = self.store.mark_stopped(str(first["id"]), "stop receipt")
        self.assertEqual(stopped["status"], "stopped")

    def test_recovery_quarantines_starting_and_failed_rows_without_reviving_them(self) -> None:
        starting = self._reserve()
        quarantined = self.store.mark_recovery_required(str(starting["id"]), "startup lost")
        self.assertEqual(quarantined["status"], "recovery_required")
        self._assert_error(
            lambda: self.store.activate(str(self.admin["id"]), str(starting["id"])),
            status=409,
            code="preview_not_starting",
        )
        self.assertEqual(
            self.store.mark_stopped(str(starting["id"]), "stop receipt")["status"],
            "stopped",
        )

        failed = self._reserve(request_id="failed")
        self.assertEqual(
            self.store.mark_failed(str(failed["id"]), "image failed")["status"],
            "failed",
        )
        self.assertEqual(
            self.store.mark_stopped(str(failed["id"]), "stop receipt")["status"],
            "stopped",
        )

    def test_public_status_and_snapshot_never_expose_host_paths_or_access_tokens(self) -> None:
        candidate = self._reserve()
        public = self.store.public_status(str(self.admin["id"]), self.project_id)
        snapshot = self.store.snapshot(str(candidate["id"]))
        for payload in (public, snapshot, candidate):
            serialized = repr(payload)
            for secret_name in ("ticket", "leaseToken", "workspacePath", "repositoryPath"):
                self.assertNotIn(secret_name, serialized)
        self.assertEqual(public["repositoryHead"], "a" * 40)

    def test_ticket_is_one_use_and_lease_rechecks_login_membership_and_role(self) -> None:
        candidate = self._reserve()
        self.store.activate(str(self.admin["id"]), str(candidate["id"]))
        viewer_login = self._login(self.viewer, "viewer-password-123")
        viewer_ticket = self.store.issue_ticket(
            str(self.viewer["id"]), self.project_id, str(viewer_login["sessionId"])
        )
        viewer_lease = self.store.consume_ticket(
            str(candidate["id"]), str(viewer_ticket["ticket"])
        )
        self.assertEqual(
            self.store.authorize_lease(
                str(candidate["id"]), str(viewer_lease["leaseToken"]), action="read"
            )["role"],
            "viewer",
        )
        self._assert_error(
            lambda: self.store.authorize_lease(
                str(candidate["id"]), str(viewer_lease["leaseToken"]), action="write"
            ),
            status=403,
            code="forbidden",
        )
        self._assert_error(
            lambda: self.store.consume_ticket(
                str(candidate["id"]), str(viewer_ticket["ticket"])
            ),
            status=401,
            code="preview_ticket_invalid",
        )

        bob_login = self._login(self.bob, "bob-password-123")
        bob_ticket = self.store.issue_ticket(
            str(self.bob["id"]), self.project_id, str(bob_login["sessionId"])
        )
        bob_lease = self.store.consume_ticket(str(candidate["id"]), str(bob_ticket["ticket"]))
        self.assertEqual(
            self.store.authorize_lease(
                str(candidate["id"]), str(bob_lease["leaseToken"]), action="write"
            )["role"],
            "contributor",
        )
        self.identity.remove_project_member(
            str(self.admin["id"]), self.project_id, str(self.bob["id"])
        )
        self.identity.add_project_member(
            str(self.admin["id"]), self.project_id, str(self.bob["id"]), "contributor"
        )
        self._assert_error(
            lambda: self.store.authorize_lease(
                str(candidate["id"]), str(bob_lease["leaseToken"])
            ),
            status=401,
            code="preview_lease_invalid",
        )

    def test_ticket_and_lease_require_current_login_and_expire(self) -> None:
        candidate = self._reserve()
        self.store.activate(str(self.admin["id"]), str(candidate["id"]))

        login = self._login(self.admin, "admin-password-123")
        ticket = self.store.issue_ticket(
            str(self.admin["id"]), self.project_id, str(login["sessionId"])
        )
        self.identity.logout(str(login["token"]))
        self._assert_error(
            lambda: self.store.consume_ticket(str(candidate["id"]), str(ticket["ticket"])),
            status=401,
            code="preview_ticket_invalid",
        )

        fresh = self._login(self.admin, "admin-password-123")
        expired_ticket = self.store.issue_ticket(
            str(self.admin["id"]), self.project_id, str(fresh["sessionId"])
        )
        self.clock.now_ms += 30_001
        self._assert_error(
            lambda: self.store.consume_ticket(
                str(candidate["id"]), str(expired_ticket["ticket"])
            ),
            status=401,
            code="preview_ticket_invalid",
        )

        current = self._login(self.admin, "admin-password-123")
        current_ticket = self.store.issue_ticket(
            str(self.admin["id"]), self.project_id, str(current["sessionId"])
        )
        current_lease = self.store.consume_ticket(
            str(candidate["id"]), str(current_ticket["ticket"])
        )
        self.clock.now_ms += 15 * 60_000 + 1
        self._assert_error(
            lambda: self.store.authorize_lease(
                str(candidate["id"]), str(current_lease["leaseToken"])
            ),
            status=401,
            code="preview_lease_invalid",
        )

        # Capability rows are disposable metadata.  A later write prunes
        # expired tickets and leases so session/deployment cleanup does not
        # retain stale hashes indefinitely.
        fresh_ticket = self.store.issue_ticket(
            str(self.admin["id"]), self.project_id, str(current["sessionId"])
        )
        self.assertTrue(fresh_ticket["ticket"])
        with sqlite_connection(self.identity.db_path, foreign_keys=True) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM team_preview_tickets").fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM team_preview_leases").fetchone()[0],
                0,
            )

    def test_disabled_account_invalidates_existing_lease(self) -> None:
        candidate = self._reserve()
        self.store.activate(str(self.admin["id"]), str(candidate["id"]))
        login = self._login(self.alice, "alice-password-123")
        ticket = self.store.issue_ticket(
            str(self.alice["id"]), self.project_id, str(login["sessionId"])
        )
        lease = self.store.consume_ticket(str(candidate["id"]), str(ticket["ticket"]))
        self.identity.set_member_active(
            str(self.admin["id"]), str(self.alice["id"]), False
        )
        self._assert_error(
            lambda: self.store.authorize_lease(
                str(candidate["id"]), str(lease["leaseToken"])
            ),
            status=401,
            code="preview_lease_invalid",
        )

    def test_cross_project_and_cross_deployment_tokens_are_rejected(self) -> None:
        second_project = self.identity.create_project(str(self.admin["id"]), "Other site")
        second_id = str(second_project["id"])
        self._insert_project(second_id, "main", "c" * 40)

        first = self._reserve()
        self.store.activate(str(self.admin["id"]), str(first["id"]))
        login = self._login(self.admin, "admin-password-123")
        first_ticket = self.store.issue_ticket(
            str(self.admin["id"]), self.project_id, str(login["sessionId"])
        )
        first_lease = self.store.consume_ticket(str(first["id"]), str(first_ticket["ticket"]))
        self._assert_error(
            lambda: self.store.consume_ticket(str(first["id"]), str(first_ticket["ticket"])),
            status=401,
            code="preview_ticket_invalid",
        )
        self._assert_error(
            lambda: self.store.authorize_lease(
                "pv-not-the-deployment", str(first_lease["leaseToken"])
            ),
            status=401,
            code="preview_lease_invalid",
        )
        self.assertEqual(
            self.store.public_status(str(self.admin["id"]), second_id)["active"],
            None,
        )


if __name__ == "__main__":
    unittest.main()
