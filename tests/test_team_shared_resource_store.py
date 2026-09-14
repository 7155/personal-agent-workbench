from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rag_ime.team.errors import TeamError
from rag_ime.team.grants import TeamGrantStore
from rag_ime.team.identity import TeamIdentityStore
from rag_ime.team import identity as identity_module
from rag_ime.team.shared_packages import StagedTeamPackage
from rag_ime.team.shared_resource_store import TeamSharedResourceStore


class TeamSharedResourceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-team-shared-resources-")
        root = Path(self.tmp.name)
        self.identity = TeamIdentityStore(root / "team.sqlite")
        self.admin = self.identity.bootstrap_admin("admin", "admin-password-123", "Admin")
        self.owner = self.identity.create_member(
            self.admin["id"], "owner", "owner-password-123", "Owner"
        )
        self.maintainer = self.identity.create_member(
            self.admin["id"], "maintainer", "maintainer-password-123", "Maintainer"
        )
        self.contributor = self.identity.create_member(
            self.admin["id"], "contributor", "contributor-password-123", "Contributor"
        )
        self.viewer = self.identity.create_member(
            self.admin["id"], "viewer", "viewer-password-123", "Viewer"
        )
        self.outsider = self.identity.create_member(
            self.admin["id"], "outsider", "outsider-password-123", "Outsider"
        )
        self.project = self.identity.create_project(self.owner["id"], "Shared project")
        self.space_id = str(self.project["id"])
        for member, role in (
            (self.maintainer, "maintainer"),
            (self.contributor, "contributor"),
            (self.viewer, "viewer"),
        ):
            self.identity.add_project_member(self.owner["id"], self.space_id, member["id"], role)
        self.grants = TeamGrantStore(self.identity.db_path, self.identity)
        self._workspace_root = root / "workspaces"
        self._workspace_root.mkdir()
        legacy_path = self._workspace_root / "legacy"
        legacy_path.mkdir()
        self.grants.bind_session(
            self.owner["id"], self.space_id, "agent:legacy", legacy_path
        )
        self.store = TeamSharedResourceStore(self.identity, grants=self.grants)
        self.sessions: dict[str, tuple[str, Path]] = {}
        for user, name, audience in (
            (self.owner, "owner-root", "project"),
            (self.owner, "owner-child", "owner"),
            (self.contributor, "contributor-root", "owner"),
        ):
            session_id = f"agent:{name}"
            path = self._workspace_root / name
            path.mkdir()
            self.grants.bind_session(user["id"], self.space_id, session_id, path, audience=audience)
            self.sessions[name] = (session_id, path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _package(
        self,
        package_id: str = "@paw/review",
        version: str = "1.0.0",
        digest: str = "a" * 64,
        *,
        label: str = "Review",
    ) -> StagedTeamPackage:
        return StagedTeamPackage(
            package_id=package_id,
            version=version,
            digest=digest,
            path=Path(self.tmp.name) / "private-staged-source",
            public_metadata={
                "packageId": package_id,
                "version": version,
                "displayName": label,
                "description": "A fixed test package",
                "source": {"kind": "bundled", "label": "Product bundle"},
            },
            manifest={"id": package_id, "version": version},
        )

    def _publish(self, package: StagedTeamPackage | None = None) -> dict[str, object]:
        return self.store.publish(self.admin["id"], package or self._package())

    def test_publish_is_admin_only_immutable_and_idempotent(self) -> None:
        published = self._publish()
        self.assertEqual(published["status"], "published")
        self.assertNotIn("path", json.dumps(published))
        self.assertNotIn(str(self.tmp.name), json.dumps(published))
        self.assertEqual(self.store.publish(self.admin["id"], self._package()), published)

        with self.assertRaises(TeamError) as changed:
            self.store.publish(self.admin["id"], self._package(digest="b" * 64))
        self.assertEqual((changed.exception.status, changed.exception.code), (409, "resource_version_conflict"))

        with self.assertRaises(TeamError) as forbidden:
            self.store.publish(self.owner["id"], self._package("@paw/other", "1.0.0", "c" * 64))
        self.assertEqual((forbidden.exception.status, forbidden.exception.code), (403, "forbidden"))

        listing = self.store.list_published(self.viewer["id"])
        self.assertEqual([item["publicationId"] for item in listing], [published["publicationId"]])
        self.assertEqual(listing[0]["metadata"]["displayName"], "Review")

    def test_status_only_changes_distribution_and_selection_requires_published_versions(self) -> None:
        first = self._publish()
        second = self._publish(self._package("@paw/other", "2.0.0", "b" * 64, label="Other"))
        selected = self.store.select(
            self.owner["id"], self.space_id, 0, [first["publicationId"], second["publicationId"]]
        )
        self.assertEqual(selected["revision"], 1)
        self.assertEqual(selected["publicationIds"], [first["publicationId"], second["publicationId"]])

        withdrawn = self.store.set_status(self.admin["id"], first["publicationId"], "withdrawn")
        self.assertEqual(withdrawn["status"], "withdrawn")
        self.assertEqual(withdrawn["packageId"], first["packageId"])
        self.assertEqual(withdrawn["digest"], first["digest"])
        self.assertEqual(self.store.selection(self.viewer["id"], self.space_id)["publicationIds"], selected["publicationIds"])

        with self.assertRaises(TeamError) as withdrawn_select:
            self.store.select(self.owner["id"], self.space_id, 1, [first["publicationId"]])
        self.assertEqual((withdrawn_select.exception.status, withdrawn_select.exception.code), (409, "resource_not_published"))
        with self.assertRaises(TeamError) as viewer_select:
            self.store.select(self.viewer["id"], self.space_id, 1, [second["publicationId"]])
        self.assertEqual((viewer_select.exception.status, viewer_select.exception.code), (403, "forbidden"))

    def test_selection_cas_is_atomic_and_bounded(self) -> None:
        published = self._publish()
        ids = [published["publicationId"]]

        def choose() -> tuple[str, str]:
            try:
                result = self.store.select(self.owner["id"], self.space_id, 0, ids)
                return "ok", str(result["revision"])
            except TeamError as error:
                return "error", error.code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: choose(), range(2)))
        self.assertEqual(sum(result[0] == "ok" for result in results), 1)
        self.assertEqual(sum(result == ("error", "resource_selection_conflict") for result in results), 1)

        with self.assertRaises(TeamError) as too_many:
            self.store.select(self.owner["id"], self.space_id, 1, [published["publicationId"]] * 17)
        self.assertEqual((too_many.exception.status, too_many.exception.code), (400, "selection_limit"))

    def test_migration_gives_existing_binding_an_empty_revision_zero_snapshot(self) -> None:
        legacy_db = Path(self.tmp.name) / "legacy.sqlite"
        migration_copy = Path(self.tmp.name) / "legacy-migrations"
        migration_copy.mkdir()
        source_migrations = Path(identity_module.__file__).with_name("migrations")
        for migration in source_migrations.glob("*.sql"):
            if migration.name != "0011_shared_resources.sql":
                shutil.copyfile(migration, migration_copy / migration.name)
        with mock.patch.object(identity_module, "_MIGRATIONS_DIR", migration_copy):
            legacy_identity = TeamIdentityStore(legacy_db)
            legacy_admin = legacy_identity.bootstrap_admin("legacy", "legacy-password-123")
            legacy_project = legacy_identity.create_project(legacy_admin["id"], "Legacy")
            legacy_grants = TeamGrantStore(legacy_db, legacy_identity)
            legacy_workspace = Path(self.tmp.name) / "legacy-workspace"
            legacy_workspace.mkdir()
            legacy_grants.bind_session(
                legacy_admin["id"], legacy_project["id"], "agent:legacy", legacy_workspace
            )
        TeamSharedResourceStore(legacy_identity)
        conn = sqlite3.connect(legacy_db)
        try:
            with conn:
                row = conn.execute(
                    "SELECT session_id FROM team_session_resource_snapshots WHERE session_id = ?",
                    ("agent:legacy",),
                ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(
            TeamSharedResourceStore(legacy_identity).session_snapshot(
                legacy_admin["id"], legacy_project["id"], "agent:legacy"
            )["selectionRevision"],
            0,
        )

    def test_root_and_child_snapshots_are_immutable_and_survive_withdrawal_and_restart(self) -> None:
        published = self._publish()
        self.store.select(self.owner["id"], self.space_id, 0, [published["publicationId"]])
        root_session = self.sessions["owner-root"][0]
        root_snapshot = self.store.capture_session(self.owner["id"], self.space_id, root_session)
        self.assertEqual(root_snapshot["selectionRevision"], 1)
        self.store.set_status(self.admin["id"], published["publicationId"], "withdrawn")

        child_session = self.sessions["owner-child"][0]
        child_snapshot = self.store.capture_session(
            self.owner["id"], self.space_id, child_session, parent_session_id=root_session
        )
        self.assertEqual(child_snapshot["publicationIds"], root_snapshot["publicationIds"])
        self.assertEqual(child_snapshot["items"], root_snapshot["items"])
        self.assertEqual(self.store.capture_session(self.owner["id"], self.space_id, child_session, parent_session_id=root_session), child_snapshot)
        self.assertEqual(TeamSharedResourceStore(self.identity).session_snapshot(self.owner["id"], self.space_id, child_session), child_snapshot)

        with self.assertRaises(TeamError) as new_root:
            self.store.capture_session(self.contributor["id"], self.space_id, self.sessions["contributor-root"][0])
        self.assertEqual((new_root.exception.status, new_root.exception.code), (409, "resource_selection_stale"))

    def test_snapshot_requires_current_grant_and_parent_owner_cannot_be_forged(self) -> None:
        published = self._publish()
        self.store.select(self.owner["id"], self.space_id, 0, [published["publicationId"]])
        owner_session = self.sessions["owner-root"][0]
        owner_snapshot = self.store.capture_session(self.owner["id"], self.space_id, owner_session)
        self.assertEqual(
            self.store.session_snapshot(self.viewer["id"], self.space_id, owner_session),
            owner_snapshot,
        )
        self.assertEqual(self.store.snapshot_for_binding(owner_session), owner_snapshot)

        with self.assertRaises(TeamError) as wrong_parent_owner:
            self.store.capture_session(
                self.contributor["id"], self.space_id, self.sessions["contributor-root"][0], parent_session_id=owner_session
            )
        self.assertEqual((wrong_parent_owner.exception.status, wrong_parent_owner.exception.code), (404, "resource_not_found"))

        with self.assertRaises(TeamError) as outsider:
            self.store.session_snapshot(self.outsider["id"], self.space_id, owner_session)
        self.assertEqual((outsider.exception.status, outsider.exception.code), (404, "resource_not_found"))

        self.identity.remove_project_member(self.owner["id"], self.space_id, self.contributor["id"])
        contributor_session = self.sessions["contributor-root"][0]
        with self.assertRaises(TeamError) as revoked:
            self.store.session_snapshot(self.contributor["id"], self.space_id, contributor_session)
        self.assertEqual((revoked.exception.status, revoked.exception.code), (404, "resource_not_found"))


if __name__ == "__main__":
    unittest.main()
