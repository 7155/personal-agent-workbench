from __future__ import annotations

from http.cookiejar import CookieJar
from contextlib import closing
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import threading
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
import unittest

from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.control_api.models import ControlAccessContext, ControlRequest
from rag_ime.rooms.store import AgentRoomStore
from rag_ime.team.access import AuthorizedTeamRequest
from rag_ime.team.errors import TeamError
from rag_ime.team.gateway import TeamApplication, make_team_server
from rag_ime.team.resource_policy import authorize_resources


class _Grants:
    def __init__(self, bindings: dict[str, dict[str, object]]) -> None:
        self.bindings = bindings
        self.required: list[tuple[str, str, str, str]] = []

    def binding(self, session_id: str, *, check_current: bool = True) -> dict[str, object]:
        value = self.bindings.get(session_id)
        if value is None:
            raise TeamError(404, "resource_not_found", "Session not found")
        if check_current and not bool(value.get("active", True)):
            raise TeamError(403, "execution_not_authorized", "Session is no longer active")
        return dict(value)

    def require_session(
        self,
        actor: str,
        space_id: str,
        session_id: str,
        *,
        action: str = "read",
    ) -> dict[str, object]:
        self.required.append((actor, space_id, session_id, action))
        value = self.binding(session_id, check_current=False)
        if (
            value.get("spaceId") != space_id
            or value.get("ownerUserId") != actor
            or not bool(value.get("active", True))
        ):
            raise TeamError(404, "resource_not_found", "Session not found")
        return value


class _Team:
    def __init__(self, grants: _Grants) -> None:
        self.grants = grants


class _Service:
    def __init__(self, rooms: AgentRoomStore, db_path: Path | None = None) -> None:
        self.agent = SimpleNamespace(rooms=rooms, db_path=db_path)


class TeamResourcePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix="paw-team-resource-policy-")
        root = Path(self.tmp.name)
        db_path = root / "rooms.sqlite"
        self.session_store = AgentSessionStore(db_path)
        self.session_store.initialize()
        self.room_store = AgentRoomStore(db_path)
        self.room_store.initialize()
        self.space_id = "project:policy"
        self.admin = "user:admin"
        self.alice = "user:alice"
        workspace = root / "workspace"
        workspace.mkdir()
        self.admin_session = str(
            self.session_store.create(
                title="Admin",
                mode="coordinator",
                role_id="companion-present-v1",
                workspace_roots=[str(workspace)],
                execution_mode="workspace_managed",
            )["id"]
        )
        self.alice_session = str(
            self.session_store.create(
                title="Alice",
                mode="coordinator",
                role_id="companion-firstlight-v1",
                workspace_roots=[str(workspace)],
                execution_mode="workspace_managed",
            )["id"]
        )
        self.grants = _Grants(
            {
                self.admin_session: {
                    "sessionId": self.admin_session,
                    "spaceId": self.space_id,
                    "ownerUserId": self.admin,
                    "active": True,
                },
                self.alice_session: {
                    "sessionId": self.alice_session,
                    "spaceId": self.space_id,
                    "ownerUserId": self.alice,
                    "active": True,
                },
            }
        )
        self.team = _Team(self.grants)
        self.service = _Service(self.room_store)
        self.room = self.room_store.create(
            title="Policy Room",
            routing_policy="manual_mentions",
            participants=[
                {
                    "sessionId": self.admin_session,
                    "roleId": "companion-present-v1",
                    "roleVersion": "1",
                    "displayName": "Admin",
                    "collaborationRole": "coordinator",
                },
                {
                    "sessionId": self.alice_session,
                    "roleId": "companion-firstlight-v1",
                    "roleVersion": "1",
                    "displayName": "Alice",
                    "collaborationRole": "implementer",
                },
            ],
        )
        self.admin_participant = next(
            item
            for item in self.room["participants"]
            if item["sessionId"] == self.admin_session
        )
        self.alice_participant = next(
            item
            for item in self.room["participants"]
            if item["sessionId"] == self.alice_session
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def request(
        self,
        actor: str,
        path: str,
        *,
        role: str = "contributor",
        params: dict[str, object] | None = None,
        query: dict[str, object] | None = None,
        body: dict[str, object] | None = None,
    ) -> AuthorizedTeamRequest:
        request = ControlRequest(
            "request:policy",
            path,
            params=params or {},
            query=query or {},
            body=body or {},
        )
        context = ControlAccessContext.team(
            user_id=actor,
            space_id=self.space_id,
            allowed_paths={path},
        )
        return AuthorizedTeamRequest(
            actor,
            {
                "id": self.space_id,
                "kind": "project",
                "role": role,
            },
            request,
            context,
        )

    def assert_team_error(self, call, *, status: int, code: str) -> TeamError:
        with self.assertRaises(TeamError) as caught:
            call()
        self.assertEqual(caught.exception.status, status)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_room_execution_boundaries_cannot_be_patched_and_create_is_deferred(self) -> None:
        request = self.request(
            self.admin,
            "agent.room.archive",
            role="owner",
            params={"roomId": self.room["id"]},
            body={"archived": False, "workspaceRoots": []},
        )
        self.assert_team_error(
            lambda: authorize_resources(self.team, self.service, request, request.request.body),
            status=403,
            code="team_scope_required",
        )

        create_body = {
            "participants": [],
            "workspaceRoots": ["/client-chosen"],
            "permissionPolicy": {"room": {"executionMode": "full_trust"}},
            "executionMode": "full_trust",
        }
        create = self.request(self.admin, "agent.rooms.create", role="owner", body=create_body)
        returned = authorize_resources(self.team, self.service, create, create_body)
        self.assertEqual(returned, create_body)

    def test_work_item_actor_is_bound_to_auth_user_and_contributor_cannot_assign_others(self) -> None:
        forged = self.request(
            self.alice,
            "agent.room.workItem.create",
            params={"roomId": self.room["id"]},
            body={
                "objective": "ship",
                "expectedOutput": "patch",
                "currentOwnerParticipantId": self.admin_participant["id"],
                "createdByParticipantId": self.admin_participant["id"],
                "accountableParticipantId": self.admin_participant["id"],
                "clientMessageId": "client:forged",
            },
        )
        self.assert_team_error(
            lambda: authorize_resources(self.team, self.service, forged, forged.request.body),
            status=403,
            code="team_resource_forbidden",
        )

        own = self.request(
            self.alice,
            "agent.room.workItem.create",
            params={"roomId": self.room["id"]},
            body={
                "objective": "ship",
                "expectedOutput": "patch",
                "currentOwnerParticipantId": self.alice_participant["id"],
                "clientMessageId": "client:own",
            },
        )
        returned = authorize_resources(self.team, self.service, own, own.request.body)
        self.assertEqual(returned["createdByParticipantId"], self.alice_participant["id"])

        cross_accountable = dict(own.request.body)
        cross_accountable["accountableParticipantId"] = self.admin_participant["id"]
        self.assert_team_error(
            lambda: authorize_resources(self.team, self.service, own, cross_accountable),
            status=403,
            code="team_resource_forbidden",
        )

        manager = self.request(
            self.admin,
            "agent.room.workItem.create",
            role="maintainer",
            params={"roomId": self.room["id"]},
            body={
                "objective": "delegate",
                "expectedOutput": "patch",
                "currentOwnerParticipantId": self.alice_participant["id"],
                "accountableParticipantId": self.alice_participant["id"],
                "clientMessageId": "client:managed",
            },
        )
        returned = authorize_resources(self.team, self.service, manager, manager.request.body)
        self.assertEqual(returned["createdByParticipantId"], self.admin_participant["id"])

    def test_artifact_and_reassignment_actor_fields_cannot_be_forged(self) -> None:
        artifact = self.request(
            self.alice,
            "agent.room.artifact.add",
            params={"roomId": self.room["id"]},
            body={"path": "report.txt", "participantId": self.admin_participant["id"]},
        )
        self.assert_team_error(
            lambda: authorize_resources(self.team, self.service, artifact, artifact.request.body),
            status=403,
            code="team_resource_forbidden",
        )
        artifact_body = {"path": "report.txt"}
        returned = authorize_resources(self.team, self.service, artifact, artifact_body)
        self.assertEqual(returned["participantId"], self.alice_participant["id"])

        steer = self.request(
            self.alice,
            "agent.room.participant.steer",
            params={"roomId": self.room["id"]},
            body={
                "action": "steer_participant",
                "rootId": "root:current",
                "clientActionId": "client:steer",
                "message": "continue",
            },
        )
        returned = authorize_resources(self.team, self.service, steer, steer.request.body)
        self.assertEqual(returned["participantId"], self.alice_participant["id"])
        forged_steer = dict(steer.request.body)
        forged_steer["participantId"] = self.admin_participant["id"]
        self.assert_team_error(
            lambda: authorize_resources(self.team, self.service, steer, forged_steer),
            status=403,
            code="team_resource_forbidden",
        )

        reassign = self.request(
            self.alice,
            "agent.room.workItem.reassign",
            params={"roomId": self.room["id"], "workItemId": "work:missing"},
            body={
                "actorParticipantId": self.admin_participant["id"],
                "targetParticipantId": self.alice_participant["id"],
            },
        )
        self.assert_team_error(
            lambda: authorize_resources(self.team, self.service, reassign, reassign.request.body),
            status=403,
            code="team_resource_forbidden",
        )

        managed = self.request(
            self.admin,
            "agent.room.workItem.reassign",
            role="owner",
            params={"roomId": self.room["id"], "workItemId": "work:missing"},
            body={
                "actorParticipantId": self.admin_participant["id"],
                "targetParticipantId": self.alice_participant["id"],
            },
        )
        returned = authorize_resources(self.team, self.service, managed, managed.request.body)
        self.assertEqual(returned["actorParticipantId"], self.admin_participant["id"])

        remove = self.request(
            self.admin,
            "agent.room.participant.remove",
            role="owner",
            params={"roomId": self.room["id"]},
            body={"participantId": self.alice_participant["id"]},
        )
        returned = authorize_resources(self.team, self.service, remove, remove.request.body)
        self.assertEqual(returned["actorParticipantId"], self.admin_participant["id"])

    def test_remove_uses_only_the_authenticated_users_unique_coordinator_default(self) -> None:
        workspace = Path(self.tmp.name) / "workspace"
        for role_id in ("companion-future-v1", "companion-flash-v1"):
            session = self.session_store.create(
                title=f"Admin {role_id}",
                mode="coordinator",
                role_id=role_id,
                workspace_roots=[str(workspace)],
                execution_mode="workspace_managed",
            )
            session_id = str(session["id"])
            self.grants.bindings[session_id] = {
                "sessionId": session_id,
                "spaceId": self.space_id,
                "ownerUserId": self.admin,
                "active": True,
            }
            self.room_store.add_participant(
                self.room["id"],
                session_id=session_id,
                role_id=role_id,
                role_version="1",
                display_name=f"Admin {role_id}",
                collaboration_role="implementer",
            )

        room = self.room_store.get(self.room["id"])
        coordinator = next(
            item
            for item in room["participants"]
            if item["sessionId"] == self.admin_session
        )
        target = next(
            item
            for item in room["participants"]
            if item["sessionId"] != self.admin_session
            and item["sessionId"] != self.alice_session
        )
        remove = self.request(
            self.admin,
            "agent.room.participant.remove",
            role="owner",
            params={"roomId": self.room["id"]},
            body={"participantId": target["id"]},
        )
        returned = authorize_resources(self.team, self.service, remove, remove.request.body)
        self.assertEqual(returned["actorParticipantId"], coordinator["id"])

    def test_missing_room_or_participant_is_a_safe_404(self) -> None:
        missing_room = self.request(
            self.alice,
            "agent.room.workItem.create",
            params={"roomId": "room:does-not-exist"},
            body={
                "objective": "x",
                "expectedOutput": "y",
                "currentOwnerParticipantId": self.alice_participant["id"],
                "clientMessageId": "client:missing-room",
            },
        )
        self.assert_team_error(
            lambda: authorize_resources(self.team, self.service, missing_room, missing_room.request.body),
            status=404,
            code="resource_not_found",
        )

        missing_participant = self.request(
            self.alice,
            "agent.room.workItem.create",
            params={"roomId": self.room["id"]},
            body={
                "objective": "x",
                "expectedOutput": "y",
                "currentOwnerParticipantId": "participant:does-not-exist",
                "clientMessageId": "client:missing-participant",
            },
        )
        self.assert_team_error(
            lambda: authorize_resources(self.team, self.service, missing_participant, missing_participant.request.body),
            status=404,
            code="resource_not_found",
        )

        missing_target = self.request(
            self.admin,
            "agent.room.participant.update",
            role="owner",
            params={"roomId": self.room["id"]},
            body={
                "participantId": "participant:does-not-exist",
                "collaborationRole": "reviewer",
            },
        )
        self.assert_team_error(
            lambda: authorize_resources(self.team, self.service, missing_target, missing_target.request.body),
            status=404,
            code="resource_not_found",
        )

        no_room_membership = _Grants({})
        no_member_team = _Team(no_room_membership)
        self.assert_team_error(
            lambda: authorize_resources(
                no_member_team,
                self.service,
                missing_participant,
                {
                    "objective": "x",
                    "expectedOutput": "y",
                    "currentOwnerParticipantId": self.alice_participant["id"],
                    "clientMessageId": "client:not-member",
                },
            ),
            status=404,
            code="resource_not_found",
        )

    def test_subagent_control_uses_persisted_run_parent_before_grant_check(self) -> None:
        db_path = Path(self.tmp.name) / "delegation.sqlite"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE agent_subagent_batches (
                    id TEXT PRIMARY KEY,
                    parent_session_id TEXT NOT NULL
                );
                CREATE TABLE agent_subagent_runs (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL
                );
                """
            )
            conn.executemany(
                "INSERT INTO agent_subagent_batches(id, parent_session_id) VALUES (?, ?)",
                [("batch:admin", self.admin_session), ("batch:alice", self.alice_session)],
            )
            conn.executemany(
                "INSERT INTO agent_subagent_runs(id, batch_id) VALUES (?, ?)",
                [("run:admin", "batch:admin"), ("run:alice", "batch:alice")],
            )
        service = _Service(self.room_store, db_path)
        cross_member = self.request(
            self.alice,
            "agent.subagent.control",
            params={"runId": "run:admin"},
            body={
                "sessionId": self.alice_session,
                "action": "steer",
                "clientActionId": "client:cross",
            },
        )
        self.assert_team_error(
            lambda: authorize_resources(self.team, service, cross_member, cross_member.request.body),
            status=404,
            code="resource_not_found",
        )
        self.assertEqual(self.grants.required, [])

        own = self.request(
            self.alice,
            "agent.subagent.control",
            params={"runId": "run:alice"},
            body={
                "sessionId": self.alice_session,
                "action": "steer",
                "clientActionId": "client:own",
            },
        )
        returned = authorize_resources(self.team, service, own, own.request.body)
        self.assertEqual(returned, own.request.body)
        self.assertEqual(self.grants.required[-1], (self.alice, self.space_id, self.alice_session, "write"))

        missing = self.request(
            self.alice,
            "agent.subagent.abort",
            params={"runId": "run:missing"},
            body={"sessionId": self.alice_session},
        )
        self.assert_team_error(
            lambda: authorize_resources(self.team, service, missing, missing.request.body),
            status=404,
            code="resource_not_found",
        )


class TeamResourcePolicyHTTPTests(unittest.TestCase):
    """Exercise the actor default through the actual Team HTTP gateway."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix="paw-team-resource-policy-http-")
        root = Path(self.tmp.name)
        web = root / "web"
        web.mkdir()
        (web / "index.html").write_text("<html><head></head><body>PAW</body></html>")
        self.app = TeamApplication(root / "data", web)
        self.admin = self.app.identity.bootstrap_admin("admin", "admin-password-123")
        self.server = make_team_server(self.app, host="127.0.0.1", port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.client = build_opener(HTTPCookieProcessor(CookieJar()))

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.app.close()
        self.tmp.cleanup()

    def request(
        self,
        path: str,
        body: dict[str, object] | None = None,
        *,
        csrf: str = "",
        method: str | None = None,
    ) -> tuple[int, object]:
        headers = {"Origin": self.base}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if csrf:
            headers["X-CSRF-Token"] = csrf
        request = Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with self.client.open(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def login(self) -> dict[str, object]:
        status, result = self.request(
            "/api/team/login",
            {"username": "admin", "password": "admin-password-123"},
        )
        self.assertEqual(status, 200, result)
        assert isinstance(result, dict)
        return result

    def test_remove_without_actor_field_uses_unique_admin_coordinator(self) -> None:
        project = self.app.identity.create_project(self.admin["id"], "Room removal")
        login = self.login()
        prefix = f"/team/spaces/{project['id']}"
        status, result = self.request(
            prefix + "/api/agent/rooms",
            {
                "title": "Admin-owned agents",
                "participants": [
                    {"roleId": "companion-present-v1"},
                    {"roleId": "companion-firstlight-v1"},
                    {"roleId": "companion-future-v1"},
                ],
            },
            csrf=str(login["csrfToken"]),
        )
        self.assertEqual(status, 201, result)
        assert isinstance(result, dict)
        room = result["room"]
        assert isinstance(room, dict)
        participants = room["participants"]
        assert isinstance(participants, list)
        self.assertEqual(len(participants), 3)
        self.assertEqual({item["ownerUserId"] for item in participants}, {self.admin["id"]})
        coordinator = next(
            item for item in participants if item["collaborationRole"] == "coordinator"
        )
        target = next(
            item for item in participants if item["id"] != coordinator["id"]
        )

        # The route's body contract intentionally allows only participantId.
        # resource_policy injects the authenticated owner's unique coordinator
        # before the legacy lifecycle handler executes.
        status, removed = self.request(
            prefix + f"/api/agent/rooms/{room['id']}/participants",
            {"participantId": target["id"]},
            csrf=str(login["csrfToken"]),
            method="PATCH",
        )
        self.assertEqual(status, 200, removed)
        assert isinstance(removed, dict)
        self.assertTrue(removed.get("ok"))
        self.assertEqual(removed["participant"]["id"], target["id"])


if __name__ == "__main__":
    unittest.main()
