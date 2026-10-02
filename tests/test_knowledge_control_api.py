from __future__ import annotations

import json
import hashlib
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


_LOOPBACK_URLOPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

from rag_ime.debug_server import DebugImeService, DebugRequestHandler, DebugServerConfig
from rag_ime.knowledge_control import KnowledgeControlFacade
from rag_ime.knowledge_embedding_profile import probe_knowledge_embedding_profile
from rag_ime.knowledge_library import HttpKnowledgeClient, KnowledgeLibraryConfig, KnowledgeLibraryService
from rag_ime.knowledge_library.worker import KnowledgeWorkerServer, _database_intake_validator


class _DirectWorker:
    def __init__(self, client: HttpKnowledgeClient, settings: dict[str, object]) -> None:
        self.client = client
        self._settings = settings

    def settings_provider(self) -> dict[str, object]:
        return self._settings

    def probe_embedding_profile(self, payload: dict[str, object]) -> dict[str, Any]:
        settings = {
            "knowledgeLibrary": {
                "embedding": dict(payload),
            }
        }
        return probe_knowledge_embedding_profile(settings, environ={})

    def management_call(self, operation: str, *args: object, **kwargs: object) -> dict[str, Any]:
        handler = getattr(self.client, operation)
        return handler(*args, **kwargs)


class KnowledgeControlApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rag-ime-knowledge-control-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)

        library = KnowledgeLibraryService(KnowledgeLibraryConfig(root / "Knowledge"))
        intake_db = root / "control.sqlite"
        self.worker_server = KnowledgeWorkerServer(
            ("127.0.0.1", 0), library,
            intake_validator=_database_intake_validator(intake_db),
        )
        self.worker_thread = threading.Thread(target=self.worker_server.serve_forever, daemon=True)
        self.worker_thread.start()
        worker_client = HttpKnowledgeClient(f"http://127.0.0.1:{self.worker_server.server_port}")

        self.service = DebugImeService(
            DebugServerConfig(
                db_path=root / "control.sqlite",
                seed_if_empty=False,
                knowledge_client=worker_client,
            )
        )
        self.embedding_settings = {
            "knowledgeLibrary": {
                "embedding": {
                    "provider": "local-hash",
                    "model": "deterministic-term-vector-v1",
                    "dimensions": 32,
                    "baseUrl": "",
                    "secretReference": "",
                    "queryPrefix": "",
                    "documentPrefix": "",
                    "denseBackend": "sqlite-exact",
                }
            }
        }
        self.service.knowledge_control = KnowledgeControlFacade(
            worker=_DirectWorker(worker_client, self.embedding_settings),  # type: ignore[arg-type]
            work_contract=self.service.management.work_contract,
        )
        self.service.agent_tools.knowledge_control = self.service.knowledge_control

        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = root
        self.control_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.control_thread = threading.Thread(target=self.control_server.serve_forever, daemon=True)
        self.control_thread.start()
        self.base_url = f"http://127.0.0.1:{self.control_server.server_port}/api/knowledge-bases"

    def tearDown(self) -> None:
        self.control_server.shutdown()
        self.control_server.server_close()
        self.control_thread.join(timeout=2)
        self.worker_server.shutdown()
        self.worker_server.server_close()
        self.worker_thread.join(timeout=2)
        self.service.pi_provider_auth.close()
        self.service.agent.close()
        self.service.management.close()

    def test_embedding_profile_probe_and_impact_are_secret_free(self) -> None:
        created = self._json_request(
            "POST",
            self.base_url,
            {"name": "Embedding impact", "description": "profile test"},
        )["base"]
        profile = self._json_request("GET", f"{self.base_url}/embedding-profile")
        candidate = dict(self.embedding_settings["knowledgeLibrary"]["embedding"])
        probe = self._json_request(
            "POST",
            f"{self.base_url}/embedding-probe",
            {"profile": candidate},
        )
        impact = self._json_request(
            "POST",
            f"{self.base_url}/embedding-impact",
            {"profile": candidate},
        )

        self.assertEqual("local-hash", profile["profile"]["provider"])
        self.assertTrue(probe["ready"])
        self.assertEqual(32, probe["dimensions"])
        self.assertEqual(1, impact["affectedBaseCount"])
        self.assertEqual(created["id"], impact["affectedBases"][0]["kbId"])
        self.assertTrue(impact["approvalRequiredForApply"])
        self.assertFalse(profile["secretsVisible"])
        self.assertNotIn("apiKey", json.dumps([profile, probe, impact]))

    def test_document_library_management_and_agent_tool_round_trip(self) -> None:
        listed = self._json_request("GET", self.base_url)
        self.assertEqual([], listed["items"])

        created = self._json_request(
            "POST",
            self.base_url,
            {
                "name": "Project papers",
                "description": "Large external documents",
                "chunkingConfig": {
                    "strategy": "markdown",
                    "size": 800,
                    "overlap": 80,
                    "respectHeadings": True,
                    "respectPageBoundaries": True,
                },
                "retrievalConfig": {"mode": "hybrid", "topK": 8, "threshold": 0.0},
            },
        )["base"]
        self.assertEqual(800, created["chunkingConfig"]["size"])
        kb_id = created["id"]
        updated = self._json_request(
            "PATCH",
            f"{self.base_url}/{quote(kb_id)}",
            {
                "agentEnabled": True,
                "retrievalConfig": {"mode": "lexical", "topK": 6, "threshold": 0.1},
                "expectedRevision": created["revision"],
            },
        )["base"]
        self.assertTrue(updated["agentEnabled"])

        data = (
            "# Field report\n\nThe grounding line retreated during the winter survey.\n"
            + "\n".join(f"Observation line {index}" for index in range(1, 451))
        ).encode("utf-8")
        query = urlencode(
            {
                "fileName": "field-report.md",
                "mimeType": "text/markdown",
                "parserProvider": "builtin",
            }
        )
        imported = self._raw_request(
            f"{self.base_url}/{quote(kb_id)}/documents/import?{query}",
            data,
            "text/markdown",
        )
        receipt = imported["receipt"]
        self.assertEqual("ready", receipt["status"])

        documents = self._json_request("GET", f"{self.base_url}/{quote(kb_id)}/documents")
        self.assertEqual(receipt["documentId"], documents["items"][0]["id"])
        detail = self._json_request(
            "GET",
            f"{self.base_url}/{quote(kb_id)}/documents/{quote(receipt['documentId'])}?offset=0&limit=10",
        )
        self.assertTrue(detail["artifact"]["available"])
        self.assertIn("Field report", detail["contentWindow"]["items"][0]["content"])
        self.assertTrue(detail["chunks"]["items"])
        later_lines = self._json_request(
            "GET",
            f"{self.base_url}/{quote(kb_id)}/documents/{quote(receipt['documentId'])}"
            "?offset=0&limit=1&lineOffset=200&lineLimit=25",
        )
        self.assertEqual(201, later_lines["contentWindow"]["items"][0]["lineNumber"])
        self.assertIn("Observation line", later_lines["contentWindow"]["items"][0]["content"])
        self.assertEqual(25, len(later_lines["contentWindow"]["items"]))
        self.assertTrue(later_lines["contentWindow"]["hasMore"])
        source, source_headers = self._binary_request(
            f"{self.base_url}/{quote(kb_id)}/documents/{quote(receipt['documentId'])}/source"
        )
        self.assertEqual(data, source)
        self.assertEqual(f'"{receipt["sha256"]}"', source_headers["ETag"])
        chunk_preview = self._json_request(
            "POST",
            f"{self.base_url}/{quote(kb_id)}/documents/{quote(receipt['documentId'])}/chunk-preview",
            {
                "chunkingConfig": {
                    "strategy": "fixed",
                    "size": 200,
                    "overlap": 20,
                    "separator": "\n\n",
                    "respectHeadings": True,
                    "respectPageBoundaries": True,
                },
                "limit": 2,
            },
        )
        self.assertEqual(receipt["documentId"], chunk_preview["fileId"])
        self.assertGreater(chunk_preview["total"], 0)

        asset_data = b"\x89PNG\r\n\x1a\nfacade-asset"
        asset_id = hashlib.sha256(asset_data).hexdigest()
        asset_path = self.worker_server.service.config.assets_dir / f"{asset_id}.png"
        asset_path.write_bytes(asset_data)
        self.worker_server.service.store.add_asset(
            sha256=asset_id,
            media_type="image/png",
            byte_size=len(asset_data),
            stored_path=str(asset_path),
        )
        self.worker_server.service.store.link_asset(
            document_id=receipt["documentId"],
            asset_sha256=asset_id,
            original_name="figure.png",
        )
        asset, asset_headers = self._binary_request(
            f"{self.base_url}/{quote(kb_id)}/documents/{quote(receipt['documentId'])}/assets/{asset_id}"
        )
        self.assertEqual(asset_data, asset)
        self.assertEqual("image/png", asset_headers["Content-Type"])
        self.assertEqual(f'"{asset_id}"', asset_headers["ETag"])
        search = self._json_request(
            "POST",
            f"{self.base_url}/{quote(kb_id)}/search",
            {"query": "grounding line", "topK": 5},
        )
        self.assertIn("grounding line", search["items"][0]["content"])

        session = self.service.agent.create_session({"title": "Knowledge control test"})["session"]
        tool_result = self.service.agent_tools.execute(
            {
                "schemaVersion": "rag-ime.agent-tool-call.v1",
                "sessionId": session["id"],
                "tool": "knowledge",
                "toolCallId": "tool:knowledge-control-test",
                "args": {"op": "search", "kbId": kb_id, "query": "winter"},
            }
        )
        self.assertTrue(tool_result["ok"])
        self.assertEqual(receipt["documentId"], tool_result["result"]["items"][0]["fileId"])
        self.assertNotIn("path", json.dumps(tool_result))

        opened = self._json_request(
            "GET",
            f"{self.base_url}/{quote(kb_id)}/documents/{quote(receipt['documentId'])}/content?lines=20",
        )
        self.assertTrue(any("grounding line" in item["content"] for item in opened["chunks"]))
        found = self._json_request(
            "POST",
            f"{self.base_url}/{quote(kb_id)}/documents/{quote(receipt['documentId'])}/find",
            {"query": "Field report", "lineWindow": 20},
        )
        self.assertEqual(receipt["documentId"], found["items"][0]["fileId"])
        self.assertIn("Field report", found["items"][0]["content"])
        self.assertTrue(self._json_request("GET", f"{self.base_url}/{quote(kb_id)}/jobs")["items"])
        self.assertTrue(self._json_request("GET", f"{self.base_url}/health")["available"])
        parser_ids = {item["id"] for item in self._json_request("GET", f"{self.base_url}/parsers")["items"]}
        self.assertEqual({"auto", "builtin", "mineru_local_http"}, parser_ids)

        graph_job = self._json_request(
            "POST",
            f"{self.base_url}/{quote(kb_id)}/graph/rebuild",
            {"expectedRevision": 0},
        )
        self.assertEqual("ready", graph_job["status"])
        graph = self._json_request(
            "GET",
            f"{self.base_url}/{quote(kb_id)}/graph?query=grounding&limit=30&depth=2&excludeChunks=true",
        )
        self.assertEqual("rag-ime.knowledge-graph.v1", graph["schemaVersion"])
        self.assertEqual(1, graph["stats"]["indexedDocumentCount"])
        self.assertFalse(any(node["kind"] == "chunk" for node in graph["nodes"]))

        current_for_reindex = self._json_request("GET", f"{self.base_url}/{quote(kb_id)}")["base"]
        stale = self._json_request(
            "PATCH",
            f"{self.base_url}/{quote(kb_id)}",
            {
                "expectedRevision": current_for_reindex["revision"],
                "chunkingConfig": {
                    "strategy": "fixed",
                    "size": 300,
                    "overlap": 30,
                    "respectHeadings": False,
                    "respectPageBoundaries": True,
                },
            },
        )["base"]
        self.assertTrue(stale["reindexRequired"])
        reindex_preview = self._json_request("GET", f"{self.base_url}/{quote(kb_id)}/reindex-preview")
        rebuilt = self._json_request(
            "POST",
            f"{self.base_url}/{quote(kb_id)}/rebuild",
            {
                "previewToken": reindex_preview["previewToken"],
                "payloadSha256": reindex_preview["payloadSha256"],
                "expectedRevision": reindex_preview["configRevision"],
                "confirmText": "REBUILD",
            },
        )
        self.assertEqual(1, rebuilt["ready"])

        retried = self._json_request(
            "POST",
            f"{self.base_url}/{quote(kb_id)}/documents/{quote(receipt['documentId'])}/retry",
            {"stage": "parse", "expectedRevision": documents["items"][0]["revision"]},
        )
        self.assertEqual("ready", retried["document"]["status"])
        deleted = self._json_request(
            "DELETE",
            f"{self.base_url}/{quote(kb_id)}/documents/{quote(receipt['documentId'])}",
        )
        self.assertTrue(deleted["deleted"])

        current = self._json_request("GET", f"{self.base_url}/{quote(kb_id)}")["base"]
        preview = self._json_request(
            "POST",
            f"{self.base_url}/{quote(kb_id)}/delete/preview",
            {"expectedRevision": current["revision"]},
        )
        applied = self._json_request(
            "POST",
            f"{self.base_url}/{quote(kb_id)}/delete/apply",
            {
                "expectedRevision": current["revision"],
                "previewToken": preview["previewToken"],
                "payloadSha256": preview["payloadSha256"],
                "confirmText": "delete",
            },
        )
        self.assertTrue(applied["ok"])
        self.assertEqual([], self._json_request("GET", self.base_url)["items"])

    def test_agent_tool_can_create_import_tune_search_and_rebuild_after_approval(self) -> None:
        session = self.service.agent.create_session(
            {"title": "Governed knowledge builder"}
        )["session"]

        call_sequence = 0
        def call(operation: str, **args: object) -> dict[str, Any]:
            nonlocal call_sequence
            call_sequence += 1
            return self.service.agent_tools.execute(
                {
                    "schemaVersion": "rag-ime.agent-tool-call.v1",
                    "sessionId": session["id"],
                    "tool": "knowledge",
                    "toolCallId": f"tool:knowledge:{operation}:{call_sequence}",
                    "args": {"op": operation, **args},
                }
            )["result"]

        def approve(prepared: dict[str, Any]) -> dict[str, Any]:
            approval = prepared["approval"]
            decided = self.service.agent.sessions.decide_approval(
                approval["approvalId"],
                approved=True,
                payload_sha256=approval["payloadSha256"],
            )
            return self.service.agent_tools.apply_approval(decided)

        created_pending = call(
            "create_base",
            name="Agent-built interview KB",
            description="Local acceptance fixture",
            agentEnabled=True,
            parserProvider="builtin",
            chunkingConfig={"strategy": "markdown", "size": 600, "overlap": 80},
            retrievalConfig={
                "mode": "hybrid",
                "topK": 8,
                "threshold": 0.0,
                "lexicalWeight": 1.0,
                "denseWeight": 1.0,
                "graphEnabled": True,
                "graphWeight": 0.7,
                "rrfK": 60,
                "candidateMultiplier": 4,
            },
        )
        self.assertTrue(created_pending["approvalRequired"])
        self.assertEqual([], self.service.knowledge_control.list_bases()["items"])
        created = approve(created_pending)["base"]
        kb_id = created["id"]

        imported_pending = call(
            "import_text",
            kbId=kb_id,
            expectedRevision=created["revision"],
            fileName="agentic-rag.md",
            text=(
                "# Agentic RAG\n\n"
                "Agentic retrieval rewrites a query, checks evidence, and searches again.\n\n"
                "The final answer cites the retrieved document."
            ),
            parserProvider="builtin",
        )
        self.assertEqual([], self.service.knowledge_control.list_documents(kb_id)["items"])
        imported = approve(imported_pending)
        self.assertEqual("ready", imported["receipt"]["status"])

        searched = call("search", kbId=kb_id, query="how does retrieval search again")
        self.assertIn("searches again", searched["items"][0]["content"])
        self.assertEqual("hybrid", searched["retrieval"]["mode"])

        configured_pending = call(
            "configure_base",
            kbId=kb_id,
            expectedRevision=created["revision"],
            chunkingConfig={"strategy": "fixed", "size": 300, "overlap": 30},
            retrievalConfig={
                "mode": "lexical",
                "topK": 6,
                "threshold": 0.0,
                "lexicalWeight": 1.0,
                "denseWeight": 1.0,
                "graphEnabled": False,
                "graphWeight": 0.0,
                "rrfK": 40,
                "candidateMultiplier": 3,
            },
        )
        configured = approve(configured_pending)["base"]
        self.assertTrue(configured["reindexRequired"])
        self.assertEqual("lexical", configured["retrievalConfig"]["mode"])

        rebuild_preview = call("rebuild_preview", kbId=kb_id)
        rebuilt_pending = call(
            "rebuild",
            kbId=kb_id,
            expectedRevision=rebuild_preview["configRevision"],
        )
        rebuilt = approve(rebuilt_pending)
        self.assertEqual(1, rebuilt["ready"])
        self.assertEqual(1, len(self.service.knowledge_control.list_documents(kb_id)["items"]))
        retuned_search = call("search", kbId=kb_id, query="final answer cites document")
        self.assertEqual("lexical", retuned_search["retrieval"]["mode"])
        self.assertEqual(
            6,
            retuned_search["retrieval"]["libraries"][0]["config"]["topK"],
        )

    @staticmethod
    def _json_request(method: str, url: str, payload: dict[str, object] | None = None) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with _LOOPBACK_URLOPEN(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _raw_request(url: str, data: bytes, mime_type: str) -> dict[str, Any]:
        request = Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": mime_type, "Accept": "application/json"},
        )
        with _LOOPBACK_URLOPEN(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _binary_request(url: str) -> tuple[bytes, dict[str, str]]:
        with _LOOPBACK_URLOPEN(Request(url, method="GET"), timeout=10) as response:
            return response.read(), {key: value for key, value in response.headers.items()}


if __name__ == "__main__":
    unittest.main()
