from __future__ import annotations

import io
import json
import threading
import time
import unittest
import zipfile
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from pathlib import Path
from tempfile import TemporaryDirectory

from rag_ime.knowledge_library import KnowledgeLibraryError
from rag_ime.control_api.errors import ControlApiError
from rag_ime.team.gateway import TeamApplication
from rag_ime.team.errors import TeamError


class TeamKnowledgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix="paw-team-knowledge-")
        root = Path(self.tmp.name)
        self.app = TeamApplication(root / "data", root / "web")
        self.admin = self.app.identity.bootstrap_admin("admin", "admin-password-123", "Admin")
        self.alice = self.app.identity.create_member(
            self.admin["id"], "alice", "alice-password-123", "Alice"
        )
        self.bob = self.app.identity.create_member(
            self.admin["id"], "bob", "bob-password-123", "Bob"
        )
        self.project = self.app.identity.create_project(self.admin["id"], "Shared knowledge")
        self.app.identity.add_project_member(
            self.admin["id"], self.project["id"], self.alice["id"], role="contributor"
        )
        self.app.identity.add_project_member(
            self.admin["id"], self.project["id"], self.bob["id"], role="viewer"
        )
        self.service = self.app.service(self.project)

    def tearDown(self) -> None:
        self.app.close()
        self.tmp.cleanup()

    def _as(self, user: dict[str, object]):
        return self.service.agent.sessions.as_actor(
            str(user["id"]), display_name=str(user.get("displayName") or user["username"])
        )

    def _control(self):
        return self.service.knowledge_control

    def _ready(self, kb_id: str, document_id: str) -> dict[str, object]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self._as(self.admin):
                result = self._control().document_detail(kb_id, document_id, {})
            document = result["document"]
            if document["status"] not in {"queued", "parsing", "indexing"}:
                self.assertEqual(document["status"], "ready", result)
                return document
            time.sleep(0.02)
        self.fail("team document did not finish indexing")

    def _base(self, *, agent_enabled: bool = True) -> dict[str, object]:
        with self._as(self.admin):
            return self._control().create_base(
                {
                    "name": "Project decisions" if agent_enabled else "Private notes",
                    "agentEnabled": agent_enabled,
                    "parserProvider": "builtin",
                }
            )["base"]

    def test_access_matrix_exposes_reads_and_limited_writes(self) -> None:
        policy = self.app.access
        for actor in (self.admin, self.alice, self.bob):
            with self.subTest(actor=actor["username"]):
                request = policy.authorize_http(
                    str(actor["id"]),
                    str(self.project["id"]),
                    method="POST",
                    path="/api/knowledge-bases/known/search",
                    query={},
                    body={"query": "decision"},
                )
                self.assertEqual(request.request.path_id, "knowledgeBases.search")

        policy.authorize_http(
            str(self.alice["id"]),
            str(self.project["id"]),
            method="POST",
            path="/api/knowledge-bases/known/documents/import",
            query={"fileName": "notes.md", "mimeType": "text/markdown"},
            body={},
        )
        for actor in (self.alice, self.bob):
            with self.subTest(manage_actor=actor["username"]), self.assertRaises(ControlApiError):
                policy.authorize_http(
                    str(actor["id"]),
                    str(self.project["id"]),
                    method="POST",
                    path="/api/knowledge-bases",
                    query={},
                    body={"name": "forbidden", "parserProvider": "builtin"},
                )
        with self.assertRaises(ControlApiError):
            policy.authorize_http(
                str(self.bob["id"]),
                str(self.project["id"]),
                method="POST",
                path="/api/knowledge-bases/known/documents/import",
                query={"fileName": "notes.md", "mimeType": "text/markdown"},
                body={},
            )

    def test_real_team_worker_uses_intake_validator_and_eight_mib_bound(self) -> None:
        self.assertEqual(self.service._team_knowledge.library.config.max_source_bytes, 8 * 1024 * 1024)
        base = self._base()
        with self._as(self.alice):
            imported = self._control().import_document(
                str(base["id"]),
                data=b"# Shared decision\n\nThe project marker is TEAM-KNOWLEDGE-42.\n",
                file_name="decision.md",
                mime_type="text/markdown",
                parser_provider="builtin",
            )
        document = self._ready(str(base["id"]), str(imported["receipt"]["documentId"]))
        self.assertEqual(document["status"], "ready")
        with self._as(self.bob):
            hits = self._control().search(str(base["id"]), {"query": "TEAM-KNOWLEDGE-42", "mode": "lexical"})
        self.assertIn("TEAM-KNOWLEDGE-42", str(hits))

        with self._as(self.alice), self.assertRaises(KnowledgeLibraryError) as too_large:
            self._control().import_document(
                str(base["id"]),
                data=b"x" * (8 * 1024 * 1024 + 1),
                file_name="large.md",
                mime_type="text/markdown",
                parser_provider="builtin",
            )
        self.assertEqual(too_large.exception.code, "source_too_large")
        with self._as(self.alice), self.assertRaises(KnowledgeLibraryError) as unsafe_name:
            self._control().import_document(
                str(base["id"]), data=b"path must not become a host file", file_name="../escape.md", mime_type="text/markdown", parser_provider="builtin"
            )
        self.assertEqual(unsafe_name.exception.code, "invalid_argument")
        with self._as(self.alice), self.assertRaises(TeamError) as parser_error:
            self._control().import_document(
                str(base["id"]), data=b"pdf", file_name="scan.pdf", mime_type="application/pdf", parser_provider="mineru"
            )
        self.assertEqual(parser_error.exception.code, "parser_not_allowed")
        with self._as(self.alice), self.assertRaises(KnowledgeLibraryError) as pdf_error:
            self._control().import_document(
                str(base["id"]), data=b"%PDF-1.7", file_name="scan.pdf", mime_type="application/pdf", parser_provider="auto"
            )
        self.assertEqual(pdf_error.exception.code, "pdf_not_allowed")

    def test_agent_reads_only_agent_enabled_bases(self) -> None:
        enabled = self._base(agent_enabled=True)
        disabled = self._base(agent_enabled=False)
        bases = self.service._team_knowledge.list_bases({})["bases"]
        self.assertEqual({str(item["kbId"]) for item in bases}, {str(enabled["id"])})
        with self.service.agent.sessions.as_actor(str(self.alice["id"])):
            # Authorization must use the trusted ContextVar actor even when a
            # runtime call has no optional presentation display name.
            visible_without_display = self._control().list_bases()["items"]
        self.assertEqual(
            {str(item["id"]) for item in visible_without_display},
            {str(enabled["id"]), str(disabled["id"])},
        )
        with self._as(self.alice):
            visible = self._control().list_bases()["items"]
        self.assertEqual({str(item["id"]) for item in visible}, {str(enabled["id"]), str(disabled["id"])})

    def test_cross_base_document_mutation_is_checked_before_worker_call(self) -> None:
        first = self._base()
        second = self._base(agent_enabled=False)
        with self._as(self.alice):
            imported = self._control().import_document(
                str(first["id"]), data=b"document owned by first base", file_name="first.md", mime_type="text/markdown", parser_provider="builtin"
            )
        document_id = str(imported["receipt"]["documentId"])
        self._ready(str(first["id"]), document_id)
        with self._as(self.admin), self.assertRaises(KnowledgeLibraryError) as denied:
            self._control().delete_document(str(second["id"]), document_id)
        self.assertIn(denied.exception.code, {"not_found", "scope_mismatch"})
        with self._as(self.admin):
            self.assertEqual(self._control().document_detail(str(first["id"]), document_id, {})["document"]["status"], "ready")

    def test_office_zip_preflight_is_bounded_before_builtin_parse(self) -> None:
        base = self._base()
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
            package.writestr("word/document.xml", "<document><body><p>safe</p></body></document>")
            package.writestr("word/huge.bin", b"x" * (33 * 1024 * 1024))
        with self._as(self.alice), self.assertRaises(KnowledgeLibraryError) as unsafe:
            self._control().import_document(
                str(base["id"]), data=archive.getvalue(), file_name="unsafe.docx", mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", parser_provider="builtin"
            )
        self.assertEqual(unsafe.exception.code, "unsafe_archive")

    def test_regex_find_and_model_graph_are_rejected_before_worker_execution(self) -> None:
        base = self._base()
        with self._as(self.alice), self.assertRaises(TeamError) as regex_error:
            self._control().find(
                str(base["id"]), "missing-document", {"query": "(a+)+$", "regex": True}
            )
        self.assertEqual(regex_error.exception.code, "regex_not_allowed")

        with self._as(self.admin), self.assertRaises(TeamError) as model_error:
            self._control().rebuild_graph(
                str(base["id"]), {"extractorMode": "model", "modelId": "provider/model"}
            )
        self.assertEqual(model_error.exception.code, "knowledge_model_not_allowed")
        with self._as(self.admin), self.assertRaises(TeamError) as model_id_error:
            self._control().rebuild_graph(
                str(base["id"]), {"extractorMode": "deterministic", "modelId": "provider/model"}
            )
        self.assertEqual(model_id_error.exception.code, "knowledge_model_not_allowed")

    def test_agent_tool_regex_find_is_rejected_before_the_client(self) -> None:
        with self._as(self.alice):
            session = self.service.agent.sessions.create(title="Literal Knowledge search")
        with self.assertRaises(TeamError) as denied:
            self.service.agent_tools.execute(
                {
                    "sessionId": session["id"],
                    "tool": "knowledge",
                    "args": {
                        "op": "find",
                        "kbId": "missing-base",
                        "fileId": "missing-document",
                        "patterns": ["(a+)+$"],
                        "useRegex": True,
                    },
                }
            )
        self.assertEqual(denied.exception.code, "regex_not_allowed")


class TeamKnowledgeRegexHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix="paw-team-knowledge-regex-http-")
        root = Path(self.tmp.name)
        self.app = TeamApplication(root / "data", root / "web")
        self.admin = self.app.identity.bootstrap_admin("admin", "admin-password-123", "Admin")
        self.project = self.app.identity.create_project(self.admin["id"], "Regex project")
        from rag_ime.team.gateway import make_team_server

        self.server = make_team_server(self.app, port=0)
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
        method: str = "GET",
    ) -> tuple[int, dict[str, object]]:
        headers: dict[str, str] = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers.update(
                {
                    "Content-Type": "application/json",
                    "Origin": self.base,
                    "X-CSRF-Token": csrf,
                }
            )
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            response = self.client.open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def login(self) -> dict[str, object]:
        status, result = self.request(
            "/api/team/login",
            {"username": "admin", "password": "admin-password-123"},
            method="POST",
        )
        self.assertEqual(status, 200, result)
        return result

    def test_http_regex_find_is_rejected_without_running_a_pattern(self) -> None:
        login = self.login()
        status, result = self.request(
            f"/team/spaces/{self.project['id']}/api/knowledge-bases/missing/documents/missing/find",
            {"query": "(a+)+$", "regex": True},
            csrf=str(login["csrfToken"]),
            method="POST",
        )
        self.assertIn(status, (400, 403), result)
        self.assertEqual(result.get("errorCode"), "regex_not_allowed")


if __name__ == "__main__":
    unittest.main()
