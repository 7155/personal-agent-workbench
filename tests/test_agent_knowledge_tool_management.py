from __future__ import annotations

import copy
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from rag_ime.agent_memory_sources import AgentMemorySourceStore
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.agent_tools import ControlToolGateway


class _ReadKnowledgeClient:
    def __init__(self) -> None:
        self.search_payloads = []

    def list_bases(self, _payload):
        return {"items": [{"kbId": "kb:docs", "name": "项目资料"}]}

    def search(self, payload):
        self.search_payloads.append(copy.deepcopy(dict(payload)))
        return {"items": [], "query": payload["query"]}

    def find(self, _payload):
        return {"items": []}

    def open(self, _payload):
        return {"items": []}

    def status(self, _payload):
        return {"available": True, "state": "ready"}


class _KnowledgeControl:
    def __init__(self) -> None:
        self.base = {
            "id": "kb:docs",
            "name": "项目资料",
            "description": "",
            "revision": 3,
            "agentEnabled": True,
            "parser": "builtin",
            "documentCount": 1,
            "chunkCount": 2,
            "chunkingConfig": {
                "strategy": "markdown",
                "size": 1200,
                "overlap": 160,
                "separator": "\n\n",
                "respectHeadings": True,
                "respectPageBoundaries": True,
            },
            "retrievalConfig": {
                "mode": "hybrid",
                "topK": 10,
                "threshold": 0.0,
                "lexicalWeight": 1.0,
                "denseWeight": 1.0,
                "graphEnabled": True,
                "graphWeight": 0.7,
                "rrfK": 60,
                "candidateMultiplier": 4,
                "rerankEnabled": False,
                "rerankCandidateDepth": 40,
            },
        }
        self.mutations: list[tuple[str, object]] = []

    def get_base(self, kb_id: str):
        if kb_id != self.base["id"]:
            raise ValueError("unknown knowledge base")
        return {"ok": True, "base": copy.deepcopy(self.base)}

    def list_documents(self, kb_id: str):
        if kb_id != self.base["id"]:
            raise ValueError("unknown knowledge base")
        return {
            "ok": True,
            "items": [{"id": "file:1", "baseId": kb_id, "name": "guide.md"}],
            "total": 1,
        }

    def create_base(self, payload):
        self.mutations.append(("create_base", copy.deepcopy(dict(payload))))
        return {
            "ok": True,
            "base": {
                "id": "kb:new",
                "name": payload["name"],
                "revision": 1,
                "agentEnabled": bool(payload.get("agentEnabled", False)),
            },
        }

    def update_base(self, kb_id: str, payload):
        if payload["expectedRevision"] != self.base["revision"]:
            raise ValueError("revision mismatch")
        self.mutations.append(("configure_base", copy.deepcopy(dict(payload))))
        for key in (
            "name",
            "description",
            "agentEnabled",
            "chunkingConfig",
            "retrievalConfig",
        ):
            if key in payload:
                self.base[key] = copy.deepcopy(payload[key])
        self.base["revision"] += 1
        return {"ok": True, "base": copy.deepcopy(self.base)}

    def import_document(
        self,
        kb_id: str,
        *,
        data: bytes,
        file_name: str,
        mime_type: str,
        parser_provider: str,
    ):
        self.mutations.append(
            (
                "import_text",
                {
                    "kbId": kb_id,
                    "data": bytes(data),
                    "fileName": file_name,
                    "mimeType": mime_type,
                    "parserProvider": parser_provider,
                },
            )
        )
        return {
            "ok": True,
            # The Agent gateway must fail closed if a worker accidentally
            # claims a document mutation is eligible for personal Memory.
            "personalMemoryEligible": True,
            "receipt": {
                "kbId": kb_id,
                "documentId": "file:new",
                "fileName": file_name,
                "byteSize": len(data),
            },
        }

    def reindex_preview(self, kb_id: str):
        return {
            "ok": True,
            "kbId": kb_id,
            "configRevision": self.base["revision"],
            "previewToken": f"preview:{self.base['revision']}",
            "payloadSha256": "f" * 64,
            "documentCount": self.base["documentCount"],
            "chunkCount": self.base["chunkCount"],
        }

    def rebuild(self, kb_id: str, payload):
        if payload["expectedRevision"] != self.base["revision"]:
            raise ValueError("stale rebuild")
        if payload["previewToken"] != f"preview:{self.base['revision']}":
            raise ValueError("stale rebuild preview")
        self.mutations.append(("rebuild", copy.deepcopy(dict(payload))))
        return {"ok": True, "kbId": kb_id, "rebuilt": True}


class AgentKnowledgeToolManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-agent-knowledge-")
        self.db_path = Path(self.tmp.name) / "agent.sqlite"
        self.sessions = AgentSessionStore(self.db_path)
        self.sessions.initialize()
        self.session = self.sessions.create(title="knowledge tool", created_at_ms=1)
        self.control = _KnowledgeControl()
        self.reader = _ReadKnowledgeClient()
        self.gateway = ControlToolGateway(
            sessions=self.sessions,
            management=object(),
            core=object(),
            project="wisdom-weasel-rag-ime",
            knowledge_client=self.reader,
            knowledge_control=self.control,
        )
        self.memory_sources = AgentMemorySourceStore(
            self.db_path,
            project="wisdom-weasel-rag-ime",
        )
        self.memory_sources.initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _call(self, operation: str, **args: object) -> dict[str, object]:
        self.tool_call_ordinal = getattr(self, "tool_call_ordinal", 0) + 1
        return {
            "schemaVersion": "rag-ime.agent-tool-call.v1",
            "sessionId": self.session["id"],
            "tool": "knowledge",
            "toolCallId": f"tool:knowledge:{operation}:{self.tool_call_ordinal}",
            "args": {"op": operation, **args},
        }

    def _approve_and_apply(self, prepared: dict[str, object]):
        approval = prepared["approval"]
        decided = self.sessions.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )
        return self.gateway.apply_approval(decided)

    def test_manifest_exposes_read_inspect_and_governed_management_operations(self) -> None:
        manifest = next(
            item
            for item in self.gateway.runtime_manifests(self.session)
            if item["name"] == "knowledge"
        )
        branches = {
            branch["properties"]["op"]["const"]: branch
            for branch in manifest["parameters"]["oneOf"]
        }

        self.assertEqual(
            set(branches),
            {
                "list_bases",
                "get_base",
                "list_documents",
                "search",
                "find",
                "open",
                "status",
                "create_base",
                "configure_base",
                "import_text",
                "rebuild_preview",
                "rebuild",
            },
        )
        self.assertEqual(
            branches["configure_base"]["required"],
            ["op", "kbId", "expectedRevision"],
        )
        self.assertEqual(
            branches["import_text"]["required"],
            ["op", "kbId", "expectedRevision", "fileName", "text"],
        )
        self.assertEqual(
            branches["rebuild"]["required"],
            ["op", "kbId", "expectedRevision"],
        )
        self.assertEqual(
            manifest["parameters"]["properties"]["threshold"],
            {"type": "number", "minimum": 0.0, "maximum": 1.0},
        )
        self.assertEqual(
            manifest["parameters"]["properties"]["rerankCandidateDepth"],
            {"type": "integer", "minimum": 1, "maximum": 100},
        )

    def test_search_exposes_independent_rerank_controls_to_the_knowledge_client(self) -> None:
        result = self.gateway.execute(
            self._call(
                "search",
                kbId="kb:docs",
                query="项目审批流程",
                topK=8,
                rerank=True,
                rerankCandidateDepth=40,
            )
        )["result"]

        self.assertEqual(
            {
                "kbId": "kb:docs",
                "query": "项目审批流程",
                "topK": 8,
                "rerank": True,
                "rerankCandidateDepth": 40,
            },
            self.reader.search_payloads[-1],
        )
        self.assertIn("0 条引用证据", result["summary"])

    def test_create_base_has_no_side_effect_until_hash_bound_approval(self) -> None:
        prepared = self.gateway.execute(
            self._call(
                "create_base",
                name="面试知识库",
                description="公开评测资料",
                agentEnabled=True,
                chunkingConfig={"strategy": "markdown", "size": 800, "overlap": 120},
                retrievalConfig={
                    "mode": "hybrid",
                    "topK": 8,
                    "threshold": 0.1,
                    "lexicalWeight": 1.0,
                    "denseWeight": 1.2,
                    "graphEnabled": True,
                    "graphWeight": 0.6,
                    "rrfK": 60,
                    "candidateMultiplier": 4,
                    "rerankEnabled": True,
                    "rerankCandidateDepth": 40,
                },
            )
        )["result"]

        self.assertTrue(prepared["approvalRequired"])
        self.assertEqual(self.control.mutations, [])
        receipt = self._approve_and_apply(prepared)

        self.assertTrue(receipt["mutationApplied"])
        self.assertEqual(self.control.mutations[0][0], "create_base")
        self.assertEqual(receipt["base"]["id"], "kb:new")

    def test_configure_base_binds_revision_and_rejects_a_stale_approval(self) -> None:
        prepared = self.gateway.execute(
            self._call(
                "configure_base",
                kbId="kb:docs",
                expectedRevision=3,
                retrievalConfig={
                    "mode": "hybrid",
                    "topK": 12,
                    "threshold": 0.2,
                    "lexicalWeight": 0.8,
                    "denseWeight": 1.4,
                    "graphEnabled": True,
                    "graphWeight": 0.9,
                    "rrfK": 40,
                    "candidateMultiplier": 6,
                    "rerankEnabled": True,
                    "rerankCandidateDepth": 48,
                },
            )
        )["result"]
        approval = prepared["approval"]
        decided = self.sessions.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )
        self.control.base["revision"] = 4

        with self.assertRaisesRegex(ValueError, "changed after the approval preview"):
            self.gateway.apply_approval(decided)
        self.assertEqual(self.control.mutations, [])

    def test_import_text_is_inline_bounded_and_uses_the_intake_facade_after_approval(self) -> None:
        prepared = self.gateway.execute(
            self._call(
                "import_text",
                kbId="kb:docs",
                expectedRevision=3,
                fileName="retrieval-notes.md",
                text="# 检索策略\n\n混合检索使用 RRF。",
                parserProvider="builtin",
            )
        )["result"]

        self.assertEqual(self.control.mutations, [])
        receipt = self._approve_and_apply(prepared)

        imported = self.control.mutations[0]
        self.assertEqual(imported[0], "import_text")
        self.assertEqual(imported[1]["data"], "# 检索策略\n\n混合检索使用 RRF。".encode())
        self.assertEqual(receipt["receipt"]["documentId"], "file:new")
        self.assertEqual(receipt["memoryDomain"], "document_knowledge")
        self.assertFalse(receipt["personalMemoryEligible"])
        self.assertIn("已完成", receipt["summary"])

        applied = self.sessions.complete_approval(
            str(receipt["approvalId"]),
            state="applied",
            receipt=receipt,
        )
        checkpoint = self.memory_sources.checkpoint_tool_receipt(
            applied,
            created_at_ms=10,
        )
        self.assertTrue(checkpoint["stored"])
        self.assertNotIn("evidence", checkpoint)
        self.assertEqual(
            checkpoint["source"]["metadata"]["personalMemoryExclusionReason"],
            "document_knowledge_boundary",
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM agent_memory_evidence"
                ).fetchone()[0],
                0,
            )

        with self.assertRaisesRegex(ValueError, "fileName must not contain a path"):
            self.gateway.execute(
                self._call(
                    "import_text",
                    kbId="kb:docs",
                    expectedRevision=3,
                    fileName="../secret.md",
                    text="blocked",
                )
            )

    def test_rebuild_requires_preview_revision_and_revalidates_at_apply(self) -> None:
        preview = self.gateway.execute(
            self._call("rebuild_preview", kbId="kb:docs")
        )["result"]
        self.assertEqual(preview["configRevision"], 3)

        prepared = self.gateway.execute(
            self._call("rebuild", kbId="kb:docs", expectedRevision=3)
        )["result"]
        self.assertEqual(self.control.mutations, [])
        receipt = self._approve_and_apply(prepared)

        self.assertTrue(receipt["rebuilt"])
        self.assertEqual(self.control.mutations[0][0], "rebuild")

    def test_switching_to_read_only_after_approval_blocks_knowledge_mutation(self) -> None:
        prepared = self.gateway.execute(
            self._call(
                "import_text",
                kbId="kb:docs",
                expectedRevision=3,
                fileName="notes.md",
                text="approved but not yet applied",
            )
        )["result"]
        approval = prepared["approval"]
        decided = self.sessions.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )
        self.sessions.set_runtime_policy(
            str(self.session["id"]),
            mode="assistant",
            execution_mode="read_only",
            tool_profile_version="subagent-readonly-v1",
            allowed_tools=None,
        )

        with self.assertRaisesRegex(ValueError, "read-only"):
            self.gateway.apply_approval(decided)
        self.assertEqual(self.control.mutations, [])


if __name__ == "__main__":
    unittest.main()
