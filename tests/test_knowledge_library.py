from __future__ import annotations

import io
import importlib.util
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import uuid
import zipfile
from pathlib import Path
from unittest import mock

from rag_ime.embeddings import HashingEmbeddingProvider

from rag_ime.knowledge_library import (
    KnowledgeConflictError,
    KnowledgeLibraryConfig,
    KnowledgeLibraryService,
    KnowledgeNotFoundError,
    LocalKnowledgeClient,
    ParsedAsset,
    ParsedDocument,
    SqliteDenseIndex,
    USearchDenseIndex,
)
from rag_ime.knowledge_library.dense import dense_index_from_env
from rag_ime.knowledge_library.models import DocumentParseError
from rag_ime.knowledge_library.parsers import BuiltinDocumentParser, MinerULocalParser, ZipSafetyLimits, inspect_mineru_zip
from rag_ime.knowledge_library.service import _chunk_block_heading, _chunk_strategy_blocks


class _RecordingKnowledgeReranker:
    configured = True

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def rerank(self, query, candidates, *, limit, candidate_limit=100):
        selected = [dict(item) for item in candidates[:candidate_limit]]
        self.calls.append(
            {
                "query": query,
                "candidateIds": [item["chunkId"] for item in selected],
                "limit": limit,
                "candidateLimit": candidate_limit,
            }
        )
        output = []
        for rank, item in enumerate(list(reversed(selected))[:limit], start=1):
            item["rerankScore"] = 1.0 - ((rank - 1) * 0.1)
            item["rerankRank"] = rank
            output.append(item)
        return output

    def status(self):
        return {
            "provider": "fixture-reranker",
            "configured": True,
            "fingerprint": "fixture:sha256:" + "1" * 64,
            "calls": len(self.calls),
            "errorCount": 0,
            "fallbackCount": 0,
            "independentStage": True,
            "subagentSubstitute": False,
        }


class KnowledgeLibraryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = KnowledgeLibraryService(
            KnowledgeLibraryConfig(self.root / "Knowledge", chunk_chars=400, chunk_overlap_chars=40)
        )

    def _document(self, name: str = "field-notes.md") -> Path:
        path = self.root / name
        path.write_text(
            "# Glacier field notes\n\n"
            "The grounding line retreated after a sustained ocean warming event.\n\n"
            "## Evidence\n\n"
            "Satellite velocity and elevation observations agree with the field log.\n",
            encoding="utf-8",
        )
        return path

    def test_import_search_open_and_jobs_use_isolated_store(self) -> None:
        base = self.service.create_base("Glacier papers", agent_enabled=True)
        document = self.service.import_document(base["id"], self._document())

        self.assertEqual("ready", document["status"])
        self.assertGreater(document["chunkCount"], 0)
        self.assertTrue((self.root / "Knowledge" / "knowledge.sqlite").exists())
        self.assertTrue(Path(self.service.store.get_document(document["documentId"])["stored_path"]).is_file())

        client = LocalKnowledgeClient(self.service)
        result = client.search({"query": "grounding line", "topK": 5})
        self.assertEqual(1, result["total"])
        hit = result["items"][0]
        self.assertEqual(base["id"], hit["kbId"])
        self.assertEqual(document["documentId"], hit["fileId"])
        self.assertIn("grounding line", hit["content"])

        opened = client.open({"fileId": hit["fileId"], "topK": 3})
        self.assertEqual(hit["fileId"], opened["fileId"])
        self.assertTrue(opened["items"])
        self.assertEqual("succeeded", self.service.list_jobs(base_id=base["id"])["jobs"][0]["status"])

        found = client.find(
            {
                "kbId": base["id"],
                "fileId": hit["fileId"],
                "patterns": ["grounding line", "satellite velocity"],
                "maxWindows": 4,
                "windowSize": 8,
            }
        )
        self.assertEqual(hit["fileId"], found["items"][0]["fileId"])
        self.assertIn("grounding line", found["items"][0]["content"])
        self.assertNotIn("path", str(found))

    def test_existing_and_new_knowledge_storage_is_owner_only(self) -> None:
        root = self.root / "LegacyKnowledge"
        legacy_dir = root / "files" / "legacy"
        legacy_dir.mkdir(parents=True)
        legacy_file = legacy_dir / "document.md"
        legacy_file.write_text("legacy", encoding="utf-8")
        os.chmod(root, 0o755)
        os.chmod(root / "files", 0o755)
        os.chmod(legacy_dir, 0o755)
        os.chmod(legacy_file, 0o644)

        service = KnowledgeLibraryService(KnowledgeLibraryConfig(root))
        self.addCleanup(service.close)
        base = service.create_base("Private documents")
        imported = service.import_document(base["id"], self._document("private-notes.md"))
        stored_path = Path(service.store.get_document(imported["documentId"])["stored_path"])
        artifact_path = Path(service.store.get_document(imported["documentId"])["artifact_path"])

        for directory in (root, root / "files", root / "assets", root / "artifacts", legacy_dir, stored_path.parent, artifact_path.parent):
            with self.subTest(directory=directory):
                self.assertEqual(0o700, directory.stat().st_mode & 0o777)
        for file_path in (root / "knowledge.sqlite", legacy_file, stored_path, artifact_path):
            with self.subTest(file=file_path):
                self.assertEqual(0o600, file_path.stat().st_mode & 0o777)

    def test_find_rejects_unsafe_regex(self) -> None:
        base = self.service.create_base("Regex papers", agent_enabled=True)
        document = self.service.import_document(base["id"], self._document())
        client = LocalKnowledgeClient(self.service)

        with self.assertRaisesRegex(Exception, "unsafe regular expression"):
            client.find(
                {
                    "kbId": base["id"],
                    "fileId": document["documentId"],
                    "patterns": ["(a+)+$"],
                    "useRegex": True,
                }
            )

    def test_agent_client_filters_disabled_bases_and_non_ready_documents(self) -> None:
        base = self.service.create_base("Private imports", agent_enabled=False)
        document = self.service.import_document(base["id"], self._document())
        client = LocalKnowledgeClient(self.service)

        self.assertEqual([], client.list_bases({})["items"])
        self.assertEqual([], client.search({"query": "satellite"})["items"])
        with self.assertRaises(KnowledgeNotFoundError):
            client.open({"fileId": document["documentId"]})

        self.service.update_base(base["id"], agent_enabled=True)
        self.assertEqual(1, len(client.list_bases({})["items"]))
        self.assertEqual(1, len(client.search({"query": "satellite"})["items"]))

    def test_agent_open_enforces_the_requested_knowledge_base_scope(self) -> None:
        first_base = self.service.create_base("First scope", agent_enabled=True)
        second_base = self.service.create_base("Second scope", agent_enabled=True)
        second_document = self.service.import_document(
            second_base["id"],
            self._document("second-scope.md"),
        )
        client = LocalKnowledgeClient(self.service)

        with self.assertRaisesRegex(Exception, "outside the selected knowledge base"):
            client.open(
                {
                    "kbId": first_base["id"],
                    "fileId": second_document["documentId"],
                }
            )

    def test_fts_scores_preserve_bm25_order_instead_of_flattening_every_hit(self) -> None:
        base = self.service.create_base("Ranking papers", agent_enabled=True)
        strong = self.root / "strong.md"
        weak = self.root / "weak.md"
        strong.write_text(("glacier " * 20) + ("neutral " * 20), encoding="utf-8")
        weak.write_text("glacier " + ("neutral " * 39), encoding="utf-8")
        self.service.import_document(base["id"], strong)
        self.service.import_document(base["id"], weak)

        hits = LocalKnowledgeClient(self.service).search({"query": "glacier", "topK": 5})["items"]

        self.assertEqual(["strong.md", "weak.md"], [hit["fileName"] for hit in hits])
        self.assertEqual(1.0, hits[0]["score"])
        self.assertLess(hits[1]["score"], hits[0]["score"])

    def test_duplicate_import_is_rejected_without_overwriting_existing_record(self) -> None:
        base = self.service.create_base("Duplicates")
        source = self._document()
        first = self.service.import_document(base["id"], source)
        with self.assertRaises(KnowledgeConflictError):
            self.service.import_document(base["id"], source)
        self.assertEqual(first["documentId"], self.service.list_documents(base["id"])["documents"][0]["documentId"])

    def test_image_without_mineru_becomes_failed_document_and_can_be_retried_later(self) -> None:
        base = self.service.create_base("Scans")
        image = self.root / "scan.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-image")
        document = self.service.import_document(base["id"], image)

        self.assertEqual("failed", document["status"])
        self.assertEqual("mineru_disabled", document["error"]["code"])
        self.assertEqual("failed", self.service.list_jobs(base_id=base["id"])["jobs"][0]["status"])

    def test_builtin_parser_extracts_modern_office_packages(self) -> None:
        fixtures = {
            ".docx": {
                "word/document.xml": b'<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>Document knowledge</w:t></w:r></w:p></w:body></w:document>',
            },
            ".pptx": {
                "ppt/slides/slide1.xml": b'<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:t>Slide evidence</a:t></p:sld>',
            },
            ".xlsx": {
                "xl/sharedStrings.xml": b'<sst xmlns="urn:x"><si><t>Mass balance</t></si></sst>',
                "xl/worksheets/sheet1.xml": b'<worksheet xmlns="urn:x"><sheetData><row><c t="s"><v>0</v></c><c><v>2026</v></c></row></sheetData></worksheet>',
            },
        }
        parser = BuiltinDocumentParser()
        expected = {".docx": "Document knowledge", ".pptx": "Slide evidence", ".xlsx": "Mass balance"}
        for suffix, entries in fixtures.items():
            path = self.root / f"office{suffix}"
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, value in entries.items():
                    archive.writestr(name, value)
            with self.subTest(suffix=suffix):
                parsed = parser.parse(path)
                self.assertIn(expected[suffix], parsed.text)

    def test_builtin_office_parser_rejects_path_traversal(self) -> None:
        path = self.root / "unsafe.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("../word/document.xml", "unsafe")
        with self.assertRaises(DocumentParseError):
            BuiltinDocumentParser().parse(path)

    def test_detail_exposes_bounded_artifact_chunks_pages_and_owned_assets(self) -> None:
        image_data = b"\x89PNG\r\n\x1a\npreview"

        class AssetParser:
            def parse(self, path: Path, *, mode: str = "auto") -> ParsedDocument:
                return ParsedDocument(
                    text=(
                        "# Parsed\n\nA table image follows.\n\n![table](images/table.png)\n\n"
                        "## Station summary\n\n"
                        "| Station | Speed |\n| --- | ---: |\n| A1 | 12.5 |\n| B2 | 8.2 |\n\n\f"
                        "## Extracted metrics\n\n"
                        "<table><caption>Glacier metrics</caption><tr><td>Metric</td><td>Value</td></tr>"
                        "<tr><td>Retreat</td><td>42 m</td></tr></table>"
                    ),
                    provider="mineru_local_http",
                    provider_version="test",
                    assets=(
                        ParsedAsset(
                            name="table.png",
                            media_type="image/png",
                            sha256=__import__("hashlib").sha256(image_data).hexdigest(),
                            data=image_data,
                        ),
                    ),
                    metadata={"archiveSha256": "a" * 64, "pageCount": 2},
                )

        service = KnowledgeLibraryService(
            KnowledgeLibraryConfig(self.root / "AssetKnowledge", chunk_chars=300, chunk_overlap_chars=20),
            parser_router=AssetParser(),
        )
        base = service.create_base("Parsed materials")
        document = service.import_document(base["id"], self._document("material.md"))

        detail = service.document_detail(base["id"], document["documentId"], limit=1, line_limit=2)
        self.assertTrue(detail["artifact"]["available"])
        self.assertEqual(2, len(detail["contentWindow"]["items"]))
        self.assertEqual(1, len(detail["chunks"]["items"]))
        self.assertGreaterEqual(detail["chunks"]["total"], 2)
        self.assertEqual(2, detail["document"]["pageCount"])
        self.assertEqual(2, len(detail["tables"]))
        markdown_table, html_table = detail["tables"]
        self.assertEqual("Station summary", markdown_table["title"])
        self.assertEqual(["Station", "Speed"], markdown_table["columns"])
        self.assertEqual([["A1", "12.5"], ["B2", "8.2"]], markdown_table["rows"])
        self.assertEqual("Glacier metrics", html_table["title"])
        self.assertEqual(2, html_table["page"])
        self.assertEqual(["Metric", "Value"], html_table["columns"])
        self.assertEqual([["Retreat", "42 m"]], html_table["rows"])
        asset = detail["assets"][0]
        self.assertEqual("image/png", asset["mimeType"])
        self.assertNotIn(str(self.root), str(detail))
        self.assertEqual(
            f"/api/knowledge-bases/{base['id']}/documents/{document['documentId']}/assets/{asset['assetId']}",
            asset["readPath"],
        )
        blob = service.read_document_asset(base["id"], document["documentId"], asset["assetId"])
        self.assertEqual(image_data, blob.data)

        other_base = service.create_base("Other")
        other_document = service.import_document(other_base["id"], self._document("other.md"))
        service.store.clear_document_asset_links(other_document["documentId"])
        with self.assertRaises(KnowledgeNotFoundError):
            service.read_document_asset(other_base["id"], other_document["documentId"], asset["assetId"])

    def test_table_artifacts_enforce_count_row_column_and_cell_limits(self) -> None:
        wide_header = "|".join(f"C{index}" for index in range(40))
        separator = "|".join("---" for _ in range(40))
        long_row = "|".join(["x" * 800] + [str(index) for index in range(39)])
        first_table = f"|{wide_header}|\n|{separator}|\n" + "\n".join(
            f"|{long_row}|" for _ in range(240)
        )
        extra_tables = "\n\n".join(
            f"## T{index}\n\n| A | B |\n| --- | --- |\n| {index} | value |" for index in range(40)
        )

        class TableParser:
            def parse(self, path: Path, *, mode: str = "auto") -> ParsedDocument:
                return ParsedDocument(
                    text=f"# Tables\n\n{first_table}\n\n{extra_tables}",
                    provider="mineru_local_http",
                    provider_version="test",
                )

        service = KnowledgeLibraryService(
            KnowledgeLibraryConfig(self.root / "TableLimits"),
            parser_router=TableParser(),
        )
        base = service.create_base("Bounded tables")
        document = service.import_document(base["id"], self._document("tables.md"))

        tables = service.document_detail(base["id"], document["documentId"])["tables"]

        self.assertEqual(32, len(tables))
        self.assertEqual(32, len(tables[0]["columns"]))
        self.assertEqual(200, len(tables[0]["rows"]))
        self.assertLessEqual(len(tables[0]["rows"][0][0]), 500)
        self.assertLessEqual(len(tables[0]["markdown"]), 64_000)

    def test_background_jobs_return_before_a_slow_parser_finishes(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class SlowParser:
            def parse(self, path: Path, *, mode: str = "auto") -> ParsedDocument:
                started.set()
                if not release.wait(timeout=2):
                    raise RuntimeError("test parser was not released")
                return ParsedDocument(text="# Parsed\n\nBackground result", provider="slow", provider_version="test")

        service = KnowledgeLibraryService(
            KnowledgeLibraryConfig(self.root / "BackgroundKnowledge"),
            parser_router=SlowParser(),
            background_jobs=True,
        )
        self.addCleanup(service.close)
        base = service.create_base("Background imports")

        document = service.import_document(base["id"], self._document("background.md"))

        self.assertIn(document["status"], {"queued", "parsing"})
        self.assertTrue(started.wait(timeout=1))
        self.assertIn(service.list_jobs(base_id=base["id"])["items"][0]["status"], {"queued", "running"})
        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if service.get_document(document["documentId"])["status"] == "ready":
                break
            time.sleep(0.01)
        self.assertEqual("ready", service.get_document(document["documentId"])["status"])

    def test_active_job_can_be_cancelled_without_applying_late_parser_output(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class SlowParser:
            def parse(self, path: Path, *, mode: str = "auto") -> ParsedDocument:
                started.set()
                release.wait(timeout=2)
                return ParsedDocument(text="# Late result\n\nMust not be applied", provider="slow")

        service = KnowledgeLibraryService(
            KnowledgeLibraryConfig(self.root / "CancelledKnowledge"),
            parser_router=SlowParser(),
            background_jobs=True,
        )
        self.addCleanup(service.close)
        base = service.create_base("Cancelled imports")
        document = service.import_document(base["id"], self._document("cancel.md"))
        self.assertTrue(started.wait(timeout=1))
        job = service.list_jobs(base_id=base["id"])["items"][0]

        cancelled = service.cancel_job(job["jobId"], base_id=base["id"])["job"]
        release.set()

        self.assertEqual("cancelled", cancelled["status"])
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and service.get_document(document["documentId"])["status"] != "failed":
            time.sleep(0.01)
        current = service.get_document(document["documentId"])
        self.assertEqual("failed", current["status"])
        self.assertEqual("cancelled", current["error"]["code"])
        self.assertEqual(0, current["chunkCount"])

    def test_running_job_is_recovered_after_worker_restart(self) -> None:
        root = self.root / "RecoveredKnowledge"
        config = KnowledgeLibraryConfig(root)
        first = KnowledgeLibraryService(config)
        base = first.create_base("Recovered imports")
        document = first.import_document(base["id"], self._document("recover.md"))
        row = first.store.get_document(document["documentId"])
        revision = int(row["revision"]) + 1
        first.store.update_document(document["documentId"], {"revision": revision, "status": "parsing"})
        job_id = uuid.uuid4().hex
        timestamp = int(time.time() * 1_000)
        first.store.insert_job({
            "id": job_id,
            "document_id": document["documentId"],
            "revision": revision,
            "kind": "reindex",
            "parser_mode": "builtin",
            "status": "running",
            "stage": "parsing",
            "created_at_ms": timestamp,
            "updated_at_ms": timestamp,
        })
        first.close()

        recovered = KnowledgeLibraryService(config, background_jobs=True)
        self.addCleanup(recovered.close)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if recovered.store.get_job(job_id)["status"] == "succeeded":
                break
            time.sleep(0.01)

        self.assertEqual("succeeded", recovered.store.get_job(job_id)["status"])
        self.assertEqual("ready", recovered.get_document(document["documentId"])["status"])

    def test_chunk_preview_uses_parsed_artifact_without_mutating_index(self) -> None:
        base = self.service.create_base("Preview chunks")
        document = self.service.import_document(base["id"], self._document("preview.md"))
        before = self.service.get_document(document["documentId"])

        preview = self.service.preview_chunking(
            base["id"],
            document["documentId"],
            {
                "strategy": "fixed",
                "size": 200,
                "overlap": 20,
                "separator": "\n\n",
                "respectHeadings": True,
                "respectPageBoundaries": True,
            },
            limit=2,
        )

        after = self.service.get_document(document["documentId"])
        self.assertGreater(preview["total"], 0)
        self.assertLessEqual(len(preview["items"]), 2)
        self.assertEqual(before["revision"], after["revision"])
        self.assertEqual(before["chunkCount"], after["chunkCount"])

    def test_chunking_config_marks_documents_stale_and_confirmed_rebuild_indexes_again(self) -> None:
        self.service.dense_index = SqliteDenseIndex(
            self.service.config.database_path,
            HashingEmbeddingProvider(dimensions=192),
        )
        base = self.service.create_base("Configurable", agent_enabled=True)
        document = self.service.import_document(base["id"], self._document())
        updated = self.service.update_base(
            base["id"],
            expected_revision=base["revision"],
            chunking_config={
                "strategy": "fixed",
                "size": 240,
                "overlap": 20,
                "respectHeadings": False,
                "respectPageBoundaries": True,
            },
            retrieval_config={"mode": "dense", "topK": 7, "threshold": 0.1},
        )
        self.assertTrue(updated["reindexRequired"])
        self.assertEqual("stale", self.service.get_document(document["documentId"])["status"])
        preview = self.service.reindex_preview(base["id"])
        with self.assertRaisesRegex(Exception, "confirmText"):
            self.service.rebuild_base(
                base["id"],
                preview_token=preview["previewToken"],
                expected_revision=preview["configRevision"],
                confirm_text="no",
            )
        rebuilt = self.service.rebuild_base(
            base["id"],
            preview_token=preview["previewToken"],
            expected_revision=preview["configRevision"],
            confirm_text="REBUILD",
        )
        self.assertEqual(1, rebuilt["ready"])
        self.assertEqual("reindex", self.service.list_jobs(base_id=base["id"])["items"][0]["kind"])
        dense = LocalKnowledgeClient(self.service).search(
            {"kbId": base["id"], "query": "satellite velocity", "mode": "dense", "topK": 3}
        )
        self.assertTrue(dense["items"])
        self.assertTrue(self.service.status()["dense"]["available"])

    def test_import_retry_and_rebuild_preserve_a_document_parser_override(self) -> None:
        modes: list[str] = []

        class RecordingParser:
            def parse(self, path: Path, *, mode: str = "auto") -> ParsedDocument:
                modes.append(mode)
                return ParsedDocument(
                    text="# Parsed\n\nProvider-specific artifact.",
                    provider="mineru_local_http" if mode == "mineru" else "builtin",
                    provider_version="test",
                )

        service = KnowledgeLibraryService(
            KnowledgeLibraryConfig(self.root / "ParserOverrides"),
            parser_router=RecordingParser(),
        )
        base = service.create_base("Mixed parsers", parser_mode="builtin")
        document = service.import_document(base["id"], self._document("override.pdf"), parser_mode="mineru")
        self.assertEqual("mineru_local_http", document["parserProvider"])
        artifact_dir = Path(service.store.get_document(document["documentId"])["artifact_path"]).parent
        legacy_artifact = artifact_dir / "revision-0.md"
        legacy_artifact.write_text("obsolete", encoding="utf-8")

        retried = service.retry_document(document["documentId"])
        self.assertEqual("mineru_local_http", retried["parserProvider"])
        self.assertFalse(legacy_artifact.exists())
        self.assertEqual(1, len(list(artifact_dir.glob("revision-*.md"))))
        preview = service.reindex_preview(base["id"])
        service.rebuild_base(
            base["id"],
            preview_token=preview["previewToken"],
            expected_revision=preview["configRevision"],
            confirm_text="REBUILD",
        )

        self.assertEqual(["mineru", "mineru", "mineru"], modes)
        self.assertEqual("mineru_local_http", service.get_document(document["documentId"])["parserProvider"])

    def test_chunking_and_retrieval_configuration_are_strictly_validated(self) -> None:
        with self.assertRaisesRegex(Exception, "overlap"):
            self.service.create_base(
                "Invalid overlap",
                chunking_config={"size": 300, "overlap": 300},
            )
        with self.assertRaisesRegex(Exception, "unknown retrievalConfig"):
            self.service.create_base("Invalid retrieval", retrieval_config={"reranker": True})

    def test_agent_search_honors_mode_alias_file_name_and_weighted_hybrid_diagnostics(self) -> None:
        self.service.dense_index = SqliteDenseIndex(
            self.service.config.database_path,
            HashingEmbeddingProvider(dimensions=192),
        )
        base = self.service.create_base(
            "Scoped retrieval",
            agent_enabled=True,
            retrieval_config={
                "mode": "hybrid",
                "topK": 5,
                "threshold": 0.0,
                "lexicalWeight": 2.0,
                "denseWeight": 0.5,
                "rrfK": 20,
                "candidateMultiplier": 3,
            },
        )
        alpha = self.root / "alpha.md"
        beta = self.root / "beta.md"
        alpha.write_text("shared glacier evidence from alpha", encoding="utf-8")
        beta.write_text("shared glacier evidence from beta", encoding="utf-8")
        self.service.import_document(base["id"], alpha)
        self.service.import_document(base["id"], beta)
        client = LocalKnowledgeClient(self.service)

        scoped = client.search(
            {
                "kbId": base["id"],
                "query": "shared glacier",
                "searchMode": "lexical",
                "fileName": "beta.md",
                "topK": 5,
            }
        )
        self.assertEqual(["beta.md"], [item["fileName"] for item in scoped["items"]])
        self.assertEqual("lexical", scoped["retrieval"]["mode"])

        hybrid = client.search({"kbId": base["id"], "query": "shared glacier", "mode": "hybrid"})
        self.assertTrue(hybrid["items"])
        diagnostics = hybrid["items"][0]["diagnostics"]
        self.assertEqual("weighted-rrf", diagnostics["fusion"])
        self.assertEqual(2.0, diagnostics["lexicalWeight"])
        self.assertEqual(0.5, diagnostics["denseWeight"])
        self.assertEqual(15, hybrid["retrieval"]["libraries"][0]["candidateLimit"])

    def test_agent_search_uses_base_defaults_but_caps_the_result_budget(self) -> None:
        base = self.service.create_base(
            "Bounded agent retrieval",
            agent_enabled=True,
            retrieval_config={"mode": "lexical", "topK": 100, "threshold": 0.0},
        )
        self.service.import_document(base["id"], self._document("bounded-agent.md"))

        agent_result = LocalKnowledgeClient(self.service).search(
            {"kbId": base["id"], "query": "glacier"}
        )
        management_result = self.service.search(
            "glacier",
            base_ids=(base["id"],),
        )

        self.assertEqual(
            12,
            agent_result["retrieval"]["libraries"][0]["config"]["topK"],
        )
        self.assertEqual(
            100,
            management_result["retrieval"]["libraries"][0]["config"]["topK"],
        )

    def test_product_search_runs_independent_reranker_before_final_top_k(self) -> None:
        reranker = _RecordingKnowledgeReranker()
        service = KnowledgeLibraryService(
            KnowledgeLibraryConfig(self.root / "RerankedKnowledge", chunk_chars=400),
            reranker=reranker,
        )
        self.addCleanup(service.close)
        base = service.create_base(
            "Reranked papers",
            agent_enabled=True,
            retrieval_config={
                "mode": "lexical",
                "topK": 1,
                "candidateMultiplier": 1,
                "rerankEnabled": True,
                "rerankCandidateDepth": 2,
            },
        )
        strong = self.root / "rerank-strong.md"
        weak = self.root / "rerank-weak.md"
        strong.write_text(("glacier " * 12) + "strong evidence", encoding="utf-8")
        weak.write_text("glacier weak evidence", encoding="utf-8")
        service.import_document(base["id"], strong)
        service.import_document(base["id"], weak)

        result = LocalKnowledgeClient(service).search(
            {"kbId": base["id"], "query": "glacier"}
        )

        self.assertEqual(["rerank-weak.md"], [item["fileName"] for item in result["items"]])
        self.assertEqual(2, len(reranker.calls[0]["candidateIds"]))
        self.assertEqual(2, reranker.calls[0]["candidateLimit"])
        diagnostics = result["items"][0]["diagnostics"]
        self.assertEqual(2, diagnostics["retrievalRank"])
        self.assertEqual(1, diagnostics["rerankRank"])
        self.assertTrue(diagnostics["independentRerankStage"])
        self.assertFalse(diagnostics["subagentSubstitute"])
        self.assertEqual("fixture-reranker", result["retrieval"]["reranker"]["provider"])
        self.assertEqual(2, result["retrieval"]["libraries"][0]["rerankCandidates"])

    def test_enabled_product_rerank_fails_closed_without_a_model(self) -> None:
        base = self.service.create_base(
            "Missing reranker",
            agent_enabled=True,
            retrieval_config={
                "mode": "lexical",
                "topK": 1,
                "rerankEnabled": True,
                "rerankCandidateDepth": 2,
            },
        )
        self.service.import_document(base["id"], self._document("missing-reranker.md"))

        with self.assertRaisesRegex(Exception, "not configured") as raised:
            LocalKnowledgeClient(self.service).search(
                {"kbId": base["id"], "query": "glacier"}
            )
        self.assertEqual("reranker_unavailable", raised.exception.code)

    def test_find_pages_over_more_than_130_chunks_without_truncation(self) -> None:
        target_blocks = {131: "DEEP-TARGET-FIRST", 139: "DEEP-TARGET-SECOND"}

        class LargeParser:
            def parse(self, path: Path, *, mode: str = "auto") -> ParsedDocument:
                blocks = []
                for index in range(145):
                    marker = target_blocks.get(index, "ordinary")
                    blocks.append(f"section {index} {marker} " + ("filler " * 38))
                return ParsedDocument(
                    text="\n<CUT>\n".join(blocks),
                    provider="large-test",
                    provider_version="1",
                )

        service = KnowledgeLibraryService(
            KnowledgeLibraryConfig(self.root / "LargeKnowledge", chunk_chars=200, chunk_overlap_chars=0),
            parser_router=LargeParser(),
        )
        base = service.create_base(
            "Large corpus",
            agent_enabled=True,
            chunking_config={"strategy": "separator", "separator": "\n<CUT>\n", "size": 200, "overlap": 0},
        )
        document = service.import_document(base["id"], self._document("large-source.md"))
        self.assertGreater(document["chunkCount"], 130)
        client = LocalKnowledgeClient(service)

        first = client.find(
            {"kbId": base["id"], "fileId": document["documentId"], "patterns": ["DEEP-TARGET"], "maxWindows": 1}
        )
        self.assertIn("DEEP-TARGET-FIRST", first["items"][0]["content"])
        self.assertTrue(first["hasMore"])
        second = client.find(
            {
                "kbId": base["id"],
                "fileId": document["documentId"],
                "patterns": ["DEEP-TARGET"],
                "maxWindows": 1,
                "offset": first["nextOffset"],
            }
        )
        self.assertIn("DEEP-TARGET-SECOND", second["items"][0]["content"])

    def test_open_uses_real_cumulative_line_positions(self) -> None:
        first = "\n".join(f"A{index:02d}" for index in range(1, 31))
        second = "\n".join(f"B{index:02d}" for index in range(1, 31))

        class LineParser:
            def parse(self, path: Path, *, mode: str = "auto") -> ParsedDocument:
                return ParsedDocument(text=f"{first}\n<CUT>\n{second}", provider="line-test")

        service = KnowledgeLibraryService(
            KnowledgeLibraryConfig(self.root / "LineKnowledge", chunk_chars=200, chunk_overlap_chars=0),
            parser_router=LineParser(),
        )
        base = service.create_base(
            "Line corpus",
            agent_enabled=True,
            chunking_config={"strategy": "separator", "separator": "\n<CUT>\n", "size": 200, "overlap": 0},
        )
        document = service.import_document(base["id"], self._document("line-source.md"))

        opened = LocalKnowledgeClient(service).open(
            {"fileId": document["documentId"], "line": 35, "windowSize": 3}
        )
        self.assertEqual(35, opened["lineStart"])
        self.assertEqual(37, opened["lineEnd"])
        self.assertEqual("B05\nB06\nB07", opened["items"][0]["content"])
        self.assertEqual((35, 37), (opened["items"][0]["lineStart"], opened["items"][0]["lineEnd"]))

    def test_chunking_strategies_have_distinct_deterministic_boundaries(self) -> None:
        self.assertEqual(["a", "b"], _chunk_strategy_blocks("a<CUT>b", strategy="separator", separator="<CUT>"))
        self.assertEqual(2, len(_chunk_strategy_blocks("# One\nbody\n# Two\nbody", strategy="markdown", separator="")))
        self.assertEqual(1, len(_chunk_strategy_blocks("# One\nbody\n# Two\nbody", strategy="general", separator="")))
        book = _chunk_strategy_blocks("第一章 起点\n正文\n第二章 终点\n正文", strategy="book", separator="")
        qa = _chunk_strategy_blocks("Q: Why?\nA: Because.\nQ: How?\nA: Carefully.", strategy="qa", separator="")
        laws = _chunk_strategy_blocks("第一条 总则\n内容\n第二条 范围\n内容", strategy="laws", separator="")
        self.assertEqual(2, len(book))
        self.assertEqual(2, len(qa))
        self.assertEqual(2, len(laws))
        self.assertEqual("第一章 起点", _chunk_block_heading(book[0], strategy="book"))
        self.assertEqual("Q: Why?", _chunk_block_heading(qa[0], strategy="qa"))
        self.assertEqual("第一条 总则", _chunk_block_heading(laws[0], strategy="laws"))

    def test_embedding_provider_is_created_from_environment_and_hash_is_explicitly_degraded(self) -> None:
        environment = {
            "RAG_IME_EMBEDDING_PROVIDER": "local-hash",
            "RAG_IME_EMBEDDING_DIMENSIONS": "64",
            "RAG_IME_KNOWLEDGE_DENSE_BACKEND": "sqlite-exact",
            "RAG_IME_EMBEDDING_BATCH_SIZE": "7",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            service = KnowledgeLibraryService(KnowledgeLibraryConfig(self.root / "EnvironmentKnowledge"))
        status = service.status()["dense"]
        self.assertEqual("local-hash", status["provider"]["provider"])
        self.assertEqual("deterministic-term-vector-v1", status["provider"]["model"])
        self.assertEqual(7, status["batchSize"])
        self.assertTrue(status["degraded"])
        self.assertFalse(status["provider"]["semantic"])

    def test_dense_projection_uses_provider_embed_many(self) -> None:
        class BatchProvider:
            fingerprint = "test-semantic:model-v1"

            def __init__(self) -> None:
                self.calls: list[tuple[list[str], int]] = []

            def embed(self, text: str) -> list[float]:
                raise AssertionError("per-item embedding should not be used")

            def embed_many(self, texts: list[str], *, batch_size: int = 32) -> list[list[float]]:
                self.calls.append((list(texts), batch_size))
                return [[1.0, float(index)] for index, _text in enumerate(texts)]

        provider = BatchProvider()
        dense = SqliteDenseIndex(self.root / "batch.sqlite", provider, batch_size=2)
        dense.replace_document(
            "doc",
            [
                {"id": f"chunk-{index}", "base_id": "base", "content": f"content {index}"}
                for index in range(3)
            ],
        )
        self.assertEqual([(["content 0", "content 1", "content 2"], 2)], provider.calls)
        self.assertEqual(3, dense.status()["vectorCount"])

    def test_embedding_rebuild_retains_previous_fingerprint_for_rollback(self) -> None:
        database = self.root / "embedding-rollback.sqlite"
        previous = SqliteDenseIndex(
            database,
            HashingEmbeddingProvider(dimensions=32),
        )
        replacement = SqliteDenseIndex(
            database,
            HashingEmbeddingProvider(dimensions=64),
        )
        chunks = [
            {
                "id": "chunk-rollback",
                "base_id": "base-rollback",
                "content": "embedding rollback evidence",
            }
        ]

        previous.replace_document("doc-rollback", chunks)
        replacement.replace_document("doc-rollback", chunks)

        self.assertEqual(1, replacement.status()["vectorCount"])
        self.assertEqual(1, previous.status()["vectorCount"])
        self.assertEqual(
            "chunk-rollback",
            previous.search(
                "rollback evidence",
                base_ids=("base-rollback",),
                limit=1,
            )[0][0],
        )

        replacement.delete_document("doc-rollback")
        self.assertEqual(0, replacement.status()["vectorCount"])
        self.assertEqual(0, previous.status()["vectorCount"])

    def test_usearch_projection_retains_previous_fingerprint_mapping_for_rollback(self) -> None:
        database = self.root / "ann-embedding-rollback.sqlite"
        chunks = [
            {
                "id": "chunk-ann-rollback",
                "document_id": "doc-ann-rollback",
                "base_id": "base-ann-rollback",
                "content": "ann rollback evidence",
            }
        ]
        with (
            mock.patch.object(USearchDenseIndex, "_dependencies", return_value=(object(), object())),
            mock.patch.object(USearchDenseIndex, "rebuild_base", return_value=None),
        ):
            previous = USearchDenseIndex(
                database,
                HashingEmbeddingProvider(dimensions=32),
            )
            previous.replace_document("doc-ann-rollback", chunks)
            replacement = USearchDenseIndex(
                database,
                HashingEmbeddingProvider(dimensions=64),
            )
            replacement.replace_document("doc-ann-rollback", chunks)

        self.assertEqual(1, previous._base_vector_count("base-ann-rollback"))
        self.assertEqual(1, replacement._base_vector_count("base-ann-rollback"))

    def test_usearch_legacy_chunk_unique_projection_migrates_without_losing_mapping(self) -> None:
        database = self.root / "ann-legacy-migration.sqlite"
        fingerprint = "local-hash:32:v1"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TABLE knowledge_dense_chunks ("
                "chunk_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, base_id TEXT NOT NULL, "
                "fingerprint TEXT NOT NULL, vector_json TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO knowledge_dense_chunks VALUES (?, ?, ?, ?, ?)",
                ("chunk-legacy", "doc-legacy", "base-legacy", fingerprint, "[1.0, 0.0]"),
            )
            connection.execute(
                "CREATE TABLE knowledge_ann_keys ("
                "ann_key INTEGER PRIMARY KEY AUTOINCREMENT, chunk_id TEXT NOT NULL UNIQUE, "
                "document_id TEXT NOT NULL, base_id TEXT NOT NULL, fingerprint TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO knowledge_ann_keys(chunk_id, document_id, base_id, fingerprint) "
                "VALUES (?, ?, ?, ?)",
                ("chunk-legacy", "doc-legacy", "base-legacy", fingerprint),
            )

        with (
            mock.patch.object(USearchDenseIndex, "_dependencies", return_value=(object(), object())),
            mock.patch.object(USearchDenseIndex, "rebuild_base", return_value=None),
        ):
            migrated = USearchDenseIndex(
                database,
                HashingEmbeddingProvider(dimensions=32),
            )

        self.assertEqual(1, migrated._base_vector_count("base-legacy"))
        with sqlite3.connect(database) as connection:
            unique_indexes = [
                [row[2] for row in connection.execute(f"PRAGMA index_info('{index[1]}')")]
                for index in connection.execute("PRAGMA index_list(knowledge_ann_keys)")
                if index[2] == 1
            ]
        self.assertIn(["chunk_id", "fingerprint"], unique_indexes)

    def test_dense_factory_falls_back_honestly_when_usearch_is_unavailable(self) -> None:
        with mock.patch("rag_ime.knowledge_library.dense.USearchDenseIndex._dependencies", side_effect=RuntimeError("missing usearch")):
            dense = dense_index_from_env(
                self.root / "fallback.sqlite",
                HashingEmbeddingProvider(dimensions=64),
                {"RAG_IME_KNOWLEDGE_DENSE_BACKEND": "usearch"},
            )
        status = dense.status()
        self.assertEqual("sqlite-exact-vector-scan", status["kind"])
        self.assertEqual("usearch", status["fallbackFrom"])
        self.assertFalse(status["ann"])
        self.assertTrue(status["degraded"])

    @unittest.skipUnless(importlib.util.find_spec("usearch"), "knowledge-ann extra is not installed")
    def test_usearch_ann_persists_rebuilds_searches_and_deletes_projection(self) -> None:
        class SemanticProvider:
            fingerprint = "test-semantic:two-dim:v1"

            @staticmethod
            def _vector(text: str) -> list[float]:
                return [1.0, 0.0] if "glacier" in text else [0.0, 1.0]

            def embed(self, text: str) -> list[float]:
                return self._vector(text)

            def embed_query(self, text: str) -> list[float]:
                return self._vector(text)

            def embed_many(self, texts: list[str], *, batch_size: int = 32) -> list[list[float]]:
                return [self._vector(text) for text in texts]

        database = self.root / "ann.sqlite"
        provider = SemanticProvider()
        dense = USearchDenseIndex(database, provider)
        chunks = [
            {"id": "chunk-glacier", "document_id": "doc", "base_id": "base", "content": "glacier ice"},
            {"id": "chunk-ocean", "document_id": "doc", "base_id": "base", "content": "ocean heat"},
        ]
        dense.replace_document("doc", chunks)
        self.assertEqual("chunk-glacier", dense.search("glacier", base_ids=("base",), limit=1)[0][0])
        self.assertTrue(dense.status()["projectionConsistent"])
        index_file = next(dense.index_root.rglob("*.usearch"))
        index_file.unlink()

        restored = USearchDenseIndex(database, provider)
        self.assertEqual(1, restored.status()["startupRebuiltIndexCount"])
        self.assertEqual("chunk-ocean", restored.search("ocean", base_ids=("base",), limit=1)[0][0])
        restored.delete_document("doc")
        self.assertEqual(0, restored.status()["vectorCount"])
        self.assertEqual([], list(restored.index_root.rglob("*.usearch")))


class MinerUArchiveSafetyTests(unittest.TestCase):
    def _archive(self, entries: dict[str, bytes]) -> bytes:
        target = io.BytesIO()
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
        return target.getvalue()

    def test_reads_full_markdown_and_content_addressed_assets(self) -> None:
        payload = self._archive({"result/full.md": b"# Parsed\n\nMinerU output", "result/images/chart.png": b"png"})
        text, assets, archive_hash = inspect_mineru_zip(payload)
        self.assertIn("MinerU output", text)
        self.assertEqual("chart.png", assets[0].name)
        self.assertEqual(64, len(assets[0].sha256))
        self.assertEqual(64, len(archive_hash))

    def test_rejects_path_traversal_and_archive_bombs(self) -> None:
        with self.assertRaises(DocumentParseError) as traversal:
            inspect_mineru_zip(self._archive({"../full.md": b"unsafe"}))
        self.assertEqual("unsafe_archive", traversal.exception.code)

        bomb = self._archive({"full.md": b"A" * 10_000})
        with self.assertRaises(DocumentParseError) as ratio:
            inspect_mineru_zip(bomb, limits=ZipSafetyLimits(max_compression_ratio=2.0))
        self.assertEqual("unsafe_archive", ratio.exception.code)

    def test_rejects_symlinks_and_nested_archives(self) -> None:
        target = io.BytesIO()
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("full.md", "safe")
            symlink = zipfile.ZipInfo("images/link.png")
            symlink.create_system = 3
            symlink.external_attr = 0o120777 << 16
            archive.writestr(symlink, "destination")
        with self.assertRaises(DocumentParseError):
            inspect_mineru_zip(target.getvalue())

        with self.assertRaises(DocumentParseError):
            inspect_mineru_zip(self._archive({"full.md": b"safe", "nested.zip": b"PK"}))

    def test_mineru_344_multipart_uses_supported_backend_language_and_effort(self) -> None:
        captured: dict[str, bytes] = {}
        zip_payload = self._archive({"full.md": b"# Parsed\n\nMinerU 3.4.4"})

        class Response:
            status = 200
            headers = {"Content-Type": "application/zip"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _limit: int = -1) -> bytes:
                return zip_payload

        def urlopen(request, **_kwargs):
            captured["body"] = request.data
            return Response()

        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "paper.pdf"
            pdf.write_bytes(
                b"%PDF-1.4\n"
                b"1 0 obj << /Type /Pages /Count 3 >> endobj\n"
                b"2 0 obj << /Type /Page /Parent 1 0 R >> endobj\n"
                b"3 0 obj << /Type /Page /Parent 1 0 R >> endobj\n"
                b"4 0 obj << /Type /Page /Parent 1 0 R >> endobj\n"
            )
            parsed = MinerULocalParser(urlopen=urlopen).parse(pdf)
        body = captured["body"]
        self.assertIn(b'hybrid-engine', body)
        self.assertNotIn(b'hybrid-auto-engine', body)
        self.assertIn(b'name="lang_list"\r\n\r\nch\r\n', body)
        self.assertNotIn(b'["ch"]', body)
        self.assertIn(b'name="effort"\r\n\r\nmedium\r\n', body)
        self.assertIn("MinerU 3.4.4", parsed.text)
        self.assertEqual(3, parsed.metadata["pageCount"])

    def test_mineru_http_422_is_reported_as_parser_contract_error(self) -> None:
        captured: dict[str, bytes] = {}

        def urlopen(request, **_kwargs):
            captured["body"] = request.data
            raise urllib.error.HTTPError(
                request.full_url,
                422,
                "unprocessable",
                {},
                io.BytesIO(b'{"detail":"invalid backend"}'),
            )

        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "scan.png"
            image.write_bytes(b"png")
            with self.assertRaises(DocumentParseError) as raised:
                MinerULocalParser(urlopen=urlopen).parse(image)
        self.assertEqual("mineru_http_error", raised.exception.code)
        self.assertIn("422", str(raised.exception))
        self.assertIn(b'name="effort"\r\n\r\nhigh\r\n', captured["body"])

    def test_mineru_timeout_is_distinct_from_service_unavailable(self) -> None:
        def urlopen(_request, **_kwargs):
            raise TimeoutError("slow document")

        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "slow.pdf"
            pdf.write_bytes(b"%PDF-1.4")
            with self.assertRaises(DocumentParseError) as raised:
                MinerULocalParser(urlopen=urlopen).parse(pdf)

        self.assertEqual("mineru_timeout", raised.exception.code)

    def test_default_mineru_deadline_allows_long_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = KnowledgeLibraryConfig(Path(temporary) / "Knowledge")
        self.assertEqual(7_200.0, config.mineru_timeout_seconds)


if __name__ == "__main__":
    unittest.main()
