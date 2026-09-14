from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from rag_ime.team.errors import TeamError
from rag_ime.team.gateway import TeamApplication


class TeamSharedAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-team-shared-apps-")
        self.root = Path(self.tmp.name)
        source_root = self.root / "package-source"
        source_root.mkdir()
        fixture = Path("control-center-web/extension-apps/zhanggui-wenshu/pi-package")
        shutil.copytree(fixture, source_root / "zhanggui-wenshu")
        self.catalog_path = source_root / "plugin_catalog.json"
        self.catalog_path.write_text(
            json.dumps(
                {
                    "schemaVersion": "rag-ime.plugin-catalog.v1",
                    "catalogVersion": "test",
                    "entries": [
                        {
                            "id": "@paw/zhanggui-wenshu",
                            "displayName": "掌柜问数",
                            "description": "Offline fixture",
                            "publisher": "PAW test",
                            "source": {"kind": "bundled", "label": "Test fixture"},
                            "permissions": [],
                            "compatibility": {"runtimeProtocol": "2"},
                            "security": {"reviewed": True, "networkAccess": False},
                            "versions": [
                                {
                                    "version": "0.2.0",
                                    "sourcePath": "zhanggui-wenshu",
                                }
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.app = TeamApplication(
            self.root / "data",
            self.root / "web",
            package_source_root=source_root,
            package_catalog=self.catalog_path,
        )
        self.admin = self.app.identity.bootstrap_admin("admin", "admin-password-123", "Admin")
        self.owner = self.app.identity.create_member(
            self.admin["id"], "owner", "owner-password-123", "Owner"
        )
        self.maintainer = self.app.identity.create_member(
            self.admin["id"], "maintainer", "maintainer-password-123", "Maintainer"
        )
        self.contributor = self.app.identity.create_member(
            self.admin["id"], "contributor", "contributor-password-123", "Contributor"
        )
        self.viewer = self.app.identity.create_member(
            self.admin["id"], "viewer", "viewer-password-123", "Viewer"
        )
        self.project = self.app.identity.create_project(self.owner["id"], "Shared App project")
        self.space_id = str(self.project["id"])
        self.app.identity.add_project_member(
            self.owner["id"], self.space_id, self.maintainer["id"], "maintainer"
        )
        self.app.identity.add_project_member(
            self.owner["id"], self.space_id, self.contributor["id"], "contributor"
        )
        self.app.identity.add_project_member(
            self.owner["id"], self.space_id, self.viewer["id"], "viewer"
        )

    def tearDown(self) -> None:
        self.app.close()
        self.tmp.cleanup()

    def _publish_and_select(self) -> dict[str, object]:
        publication = self.app.shared_resources.publish(
            self.admin["id"],
            {"packageId": "@paw/zhanggui-wenshu", "version": "0.2.0"},
        )
        self.app.shared_resources.store.select(
            self.owner["id"], self.space_id, 0, [publication["publicationId"]]
        )
        return publication

    def test_real_extension_app_selection_allows_owner_session_and_fixes_snapshot(self) -> None:
        publication = self._publish_and_select()
        resources = self.app.shared_resources
        preflight = resources.for_creation(self.owner["id"], self.space_id)
        checked = resources.assert_app(
            preflight, "extension:zhanggui-wenshu", "extension_app"
        )
        self.assertEqual(checked["publicationIds"], [publication["publicationId"]])
        extension = checked["items"][0]["metadata"]["extensionApp"]
        self.assertEqual(extension["id"], "extension:zhanggui-wenshu")
        self.assertEqual(extension["packageId"], "@paw/zhanggui-wenshu")
        self.assertNotIn(str(self.root), json.dumps(checked))

        service = self.app.service(self.project)
        with service.agent.sessions.as_actor(self.owner["id"]):
            session = service.agent.sessions.create(
                title="掌柜问数 task",
                surface_kind="extension_app",
                owner_app_id="extension:zhanggui-wenshu",
                surface_key="project.metrics",
            )
        snapshot = resources.store.session_snapshot(
            self.owner["id"], self.space_id, str(session["id"])
        )
        self.assertEqual(snapshot["publicationIds"], [publication["publicationId"]])
        self.assertEqual(snapshot["items"][0]["metadata"]["extensionApp"]["id"], "extension:zhanggui-wenshu")

    def test_unknown_or_unselected_extension_app_is_rejected_and_builtins_keep_existing_route(self) -> None:
        resources = self.app.shared_resources
        empty = resources.for_creation(self.owner["id"], self.space_id)
        with self.assertRaises(TeamError) as unselected:
            resources.assert_app(empty, "extension:zhanggui-wenshu", "extension_app")
        self.assertEqual((unselected.exception.status, unselected.exception.code), (403, "app_not_selected"))

        publication = self._publish_and_select()
        with self.assertRaises(TeamError) as unknown:
            resources.assert_app(
                resources.for_creation(self.owner["id"], self.space_id),
                "extension:unselected-app",
                "extension_app",
            )
        self.assertEqual((unknown.exception.status, unknown.exception.code), (403, "app_not_selected"))
        self.assertEqual(publication["packageId"], "@paw/zhanggui-wenshu")

        # Existing Trace and Agent Lab routes are App-owned surfaces with
        # their own established route/Skill contracts; Team resource
        # selection does not silently replace that routing decision.
        snapshot = resources.for_creation(self.owner["id"], self.space_id)
        self.assertEqual(resources.assert_app(snapshot, "extension:trace-agent", "extension_app"), snapshot)
        self.assertEqual(resources.assert_app(snapshot, "extension:agent-lab", "extension_app"), snapshot)

    def test_withdrawn_resource_blocks_new_root_but_child_inherits_fixed_app(self) -> None:
        publication = self._publish_and_select()
        service = self.app.service(self.project)
        with service.agent.sessions.as_actor(self.owner["id"]):
            parent = service.agent.sessions.create(
                title="Parent App task",
                surface_kind="extension_app",
                owner_app_id="extension:zhanggui-wenshu",
                surface_key="project.parent",
            )
        with self.assertRaises(TeamError) as non_owner:
            self.app.shared_resources.for_creation(
                self.contributor["id"], self.space_id, str(parent["id"])
            )
        self.assertEqual((non_owner.exception.status, non_owner.exception.code), (404, "resource_not_found"))
        self.app.shared_resources.store.set_status(
            self.admin["id"], publication["publicationId"], "withdrawn"
        )
        with self.assertRaises(TeamError) as stale_root:
            self.app.shared_resources.for_creation(self.owner["id"], self.space_id)
        self.assertEqual((stale_root.exception.status, stale_root.exception.code), (409, "resource_selection_stale"))

        parent_snapshot = self.app.shared_resources.for_creation(
            self.owner["id"], self.space_id, str(parent["id"])
        )
        self.assertEqual(parent_snapshot["publicationIds"], [publication["publicationId"]])
        self.assertEqual(parent_snapshot["items"][0]["status"], "published")
        self.app.shared_resources.assert_app(
            parent_snapshot, "extension:zhanggui-wenshu", "extension_app"
        )
        with service.agent.sessions.as_actor(self.owner["id"]):
            child = service.agent.sessions.create_child(
                str(parent["id"]),
                title="Child App task",
                surface_kind="extension_app",
                owner_app_id="extension:zhanggui-wenshu",
                surface_key="project.child",
            )
        self.assertEqual(
            self.app.shared_resources.store.session_snapshot(
                self.owner["id"], self.space_id, str(child["id"])
            )["publicationIds"],
            [publication["publicationId"]],
        )

    def test_roles_and_revoke_are_checked_before_workspace_creation(self) -> None:
        self._publish_and_select()
        resources = self.app.shared_resources
        with self.assertRaises(TeamError) as viewer:
            resources.for_creation(self.viewer["id"], self.space_id)
        self.assertEqual((viewer.exception.status, viewer.exception.code), (403, "forbidden"))
        contributor_preflight = resources.for_creation(self.contributor["id"], self.space_id)
        resources.assert_app(
            contributor_preflight, "extension:zhanggui-wenshu", "extension_app"
        )
        self.app.identity.remove_project_member(
            self.owner["id"], self.space_id, self.contributor["id"]
        )
        with self.assertRaises(TeamError) as revoked:
            resources.for_creation(self.contributor["id"], self.space_id)
        self.assertEqual((revoked.exception.status, revoked.exception.code), (404, "space_not_found"))


if __name__ == "__main__":
    unittest.main()
