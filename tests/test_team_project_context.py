from __future__ import annotations

import concurrent.futures
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import sqlite3
import threading
import unittest
from contextlib import closing

from rag_ime.team.errors import TeamError
from rag_ime.team.gateway import TeamApplication
from rag_ime.team.project_context import TeamProjectContext


class TeamProjectContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix="paw-team-project-context-")
        root = Path(self.tmp.name)
        web = root / "web"
        web.mkdir()
        (web / "index.html").write_text("<html><head></head><body>PAW</body></html>")
        self.app = TeamApplication(root / "data", web)
        self.admin = self.app.identity.bootstrap_admin(
            "admin",
            "admin-password-123",
            display_name="Project owner",
        )
        self.alice = self.app.identity.create_member(
            self.admin["id"],
            "alice",
            "alice-password-123",
            display_name="Alice",
        )
        self.context = TeamProjectContext(self.app)

    def tearDown(self) -> None:
        self.app.close()
        self.tmp.cleanup()

    def assert_error(self, call, *, status: int, code: str) -> TeamError:
        with self.assertRaises(TeamError) as caught:
            call()
        self.assertEqual(caught.exception.status, status)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def project(self, owner: str | None = None, name: str = "Registration") -> dict[str, object]:
        return self.app.identity.create_project(owner or self.admin["id"], name)

    def brief_body(self, base_revision: int, objective: str = "Ship registration") -> dict[str, object]:
        return {
            "baseRevision": base_revision,
            "objective": objective,
            "acceptanceCriteria": [
                "New email succeeds",
                "Duplicate email has an explicit error",
            ],
        }

    def test_empty_project_overview_does_not_create_repository_or_host(self) -> None:
        project = self.project()
        projects_root = self.app.data_root / "projects"
        self.assertFalse(projects_root.exists())
        with patch("rag_ime.pi.host_client.PiRuntimeHostClient.start") as start:
            overview = self.context.overview(self.admin["id"], project["id"])
        self.assertEqual(overview["project"], {
            "id": project["id"],
            "name": "Registration",
            "role": "owner",
        })
        self.assertEqual(overview["brief"]["revision"], 0)
        self.assertEqual(overview["brief"]["objective"], "")
        self.assertEqual(overview["brief"]["acceptanceCriteria"], [])
        self.assertEqual(overview["briefHistory"], [])
        self.assertEqual(overview["members"][0]["id"], self.admin["id"])
        self.assertEqual(overview["rooms"], [])
        self.assertEqual(overview["drafts"], [])
        self.assertIsNone(overview["repository"])
        self.assertFalse(overview["runtime"]["configured"])
        self.assertEqual(overview["truncated"], {
            "rooms": False,
            "workItems": False,
            "drafts": False,
            "briefHistory": False,
        })
        start.assert_not_called()
        self.assertFalse(projects_root.exists())

    def test_publish_brief_uses_atomic_cas_and_preserves_frozen_history(self) -> None:
        project = self.project()
        body = self.brief_body(0)
        barrier = threading.Barrier(2)

        def publish_once() -> tuple[str, object]:
            barrier.wait(timeout=5)
            try:
                return "ok", self.context.publish_brief(self.admin["id"], project["id"], body)
            except TeamError as error:
                return "error", error

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _item: publish_once(), range(2)))
        self.assertEqual(sum(kind == "ok" for kind, _value in results), 1)
        self.assertEqual(sum(
            kind == "error"
            and isinstance(value, TeamError)
            and value.status == 409
            and value.code == "brief_revision_conflict"
            for kind, value in results
        ), 1)

        second = self.context.publish_brief(
            self.admin["id"],
            project["id"],
            self.brief_body(1, "Require company name"),
        )
        self.assertEqual(second["revision"], 2)
        overview = self.context.overview(self.admin["id"], project["id"])
        self.assertEqual(overview["brief"]["revision"], 2)
        self.assertEqual(overview["brief"]["objective"], "Require company name")
        self.assertEqual(
            [item["revision"] for item in overview["briefHistory"]],
            [2, 1],
        )
        self.assertEqual(
            {item["updatedByDisplayName"] for item in overview["briefHistory"]},
            {"Project owner"},
        )

    def test_publish_requires_project_manager_and_does_not_treat_platform_admin_as_member(self) -> None:
        project = self.project()
        self.app.identity.add_project_member(
            self.admin["id"],
            project["id"],
            self.alice["id"],
            role="viewer",
        )
        self.assert_error(
            lambda: self.context.publish_brief(
                self.alice["id"], project["id"], self.brief_body(0)
            ),
            status=403,
            code="forbidden",
        )
        self.assertEqual(
            self.context.overview(self.alice["id"], project["id"])["project"]["role"],
            "viewer",
        )

        alice_project = self.project(self.alice["id"], "Alice's project")
        self.assert_error(
            lambda: self.context.overview(self.admin["id"], alice_project["id"]),
            status=404,
            code="space_not_found",
        )
        self.assert_error(
            lambda: self.context.publish_brief(
                self.admin["id"], alice_project["id"], self.brief_body(0)
            ),
            status=404,
            code="space_not_found",
        )
        personal = next(
            space
            for space in self.app.identity.list_spaces(self.admin["id"])
            if space["kind"] == "personal"
        )
        self.assert_error(
            lambda: self.context.overview(self.admin["id"], personal["id"]),
            status=404,
            code="space_not_found",
        )

    def test_overview_projects_room_work_metadata_without_transcript_or_workspace_path(self) -> None:
        project = self.project()
        service = self.app.service(project)
        with service.agent.sessions.as_actor(
            self.admin["id"],
            display_name="Project owner",
        ):
            room_result = service.agent.create_room(
                {
                    "title": "Registration implementation",
                    "participants": [
                        {"roleId": "companion-present-v1"},
                        {"roleId": "companion-firstlight-v1"},
                    ],
                }
            )
        room = room_result["room"]
        participant = room["participants"][0]
        work_result = service.agent.create_room_work_item(
            room["id"],
            {
                "objective": "Implement form",
                "expectedOutput": "A form draft",
                "acceptanceCriteria": ["Duplicate email is explained"],
                "createdByParticipantId": participant["id"],
                "currentOwnerParticipantId": participant["id"],
                "clientMessageId": "project-context-fixture",
            },
        )
        workspace_path = Path(
            str(self.app.grants.binding(participant["sessionId"])["workspacePath"])
        )
        (workspace_path / "draft.txt").write_text("draft metadata\n", encoding="utf-8")
        draft = self.app.workspaces.share_draft(
            participant["sessionId"],
            self.admin["id"],
            "Project context draft",
        )
        self.context.publish_brief(
            self.admin["id"],
            project["id"],
            self.brief_body(0, "Published after the initial Session baseline"),
        )
        history = service.history_root(participant["sessionId"])
        history.mkdir(parents=True, mode=0o700)
        (history / "private.jsonl").write_text("PRIVATE_TRANSCRIPT_MUST_NOT_BE_PROJECT_METADATA")
        with patch("rag_ime.pi.host_client.PiRuntimeHostClient.start") as start:
            overview = self.context.overview(self.admin["id"], project["id"])
        self.assertEqual(len(overview["rooms"]), 1)
        room_projection = overview["rooms"][0]
        self.assertEqual(room_projection["id"], room["id"])
        self.assertEqual(room_projection["title"], "Registration implementation")
        self.assertEqual(room_projection["participantCount"], 2)
        task = room_projection["workItems"][0]
        self.assertEqual(task["id"], work_result["workItem"]["id"])
        self.assertEqual(task["currentOwnerParticipantId"], participant["id"])
        self.assertEqual(task["ownerSessionId"], participant["sessionId"])
        self.assertEqual(task["currentOwnerUserId"], self.admin["id"])
        self.assertEqual(task["currentOwnerDisplayName"], "Project owner")
        self.assertEqual(task["ownerRequirementsRevision"], 0)
        self.assertTrue(task["requirementsStale"])
        self.assertEqual(task["expectedOutput"], "A form draft")
        self.assertEqual(overview["drafts"][0]["draftId"], draft["draftId"])
        self.assertEqual(overview["drafts"][0]["creatorDisplayName"], "Project owner")
        self.assertEqual(overview["drafts"][0]["requirementsRevision"], 0)
        self.assertEqual(overview["drafts"][0]["currentRequirementsRevision"], 1)
        self.assertTrue(overview["drafts"][0]["requirementsStale"])
        self.assertNotIn("PRIVATE_TRANSCRIPT", json.dumps(overview))
        self.assertNotIn(str(self.app.data_root), json.dumps(overview))
        self.assertFalse(overview["runtime"]["configured"])
        start.assert_not_called()

    def test_brief_input_is_bounded_and_revision_table_is_append_only(self) -> None:
        project = self.project()
        self.assert_error(
            lambda: self.context.publish_brief(
                self.admin["id"], project["id"],
                {**self.brief_body(0), "unexpected": True},
            ),
            status=400,
            code="invalid_input",
        )
        self.assert_error(
            lambda: self.context.publish_brief(
                self.admin["id"], project["id"],
                {**self.brief_body(0), "objective": "x" * 4_001},
            ),
            status=400,
            code="invalid_input",
        )
        self.assert_error(
            lambda: self.context.publish_brief(
                self.admin["id"], project["id"],
                {**self.brief_body(0), "acceptanceCriteria": []},
            ),
            status=400,
            code="invalid_input",
        )
        with closing(sqlite3.connect(self.app.identity.db_path)) as conn, conn:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertIn("team_project_briefs", tables)


if __name__ == "__main__":
    unittest.main()
