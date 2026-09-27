from __future__ import annotations

import tempfile
import json
import unittest
from pathlib import Path

from rag_ime.knowledge_library.models import KnowledgeLibraryConfig, ParsedBlock, ParsedDocument
from rag_ime.knowledge_library.service import KnowledgeLibraryService, DEFAULT_CHUNKING_CONFIG, _chunk_document


class StructuredChunkingTests(unittest.TestCase):
    def test_layout_page_numbers_are_retained_in_parse_tree_but_not_indexed(self) -> None:
        blocks = (ParsedBlock("paragraph", "Evidence on page one.", page=1), ParsedBlock("page_number", "1", page=1), ParsedBlock("reference", "[1] Original source.", page=1))
        parsed = ParsedDocument(text="Evidence on page one.\n1\n[1] Original source.", provider="fixture", blocks=blocks)
        chunks = _chunk_document(parsed, document_id="doc", base_id="base", chunking_config=DEFAULT_CHUNKING_CONFIG)
        self.assertNotIn("1", [row["content"] for row in chunks])
        self.assertIn("[1] Original source.", [row["content"] for row in chunks])
        self.assertEqual(parsed.blocks, blocks)

    def test_chart_data_uses_bounded_row_groups_and_preserves_extraction_evidence(self) -> None:
        chart = "| Category | Value |\n|---|---|\n" + "\n".join(f"| data-{i} | {i} |" for i in range(30))
        parsed = ParsedDocument(text=chart, provider="fixture", blocks=(ParsedBlock("chart", chart, metadata={"chartDataAvailable": True, "ocrApplied": False, "assetSha256": "a" * 64}),))
        chunks = _chunk_document(parsed, document_id="doc", base_id="base", chunking_config={**DEFAULT_CHUNKING_CONFIG, "size": 200})
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertTrue(chunk["content"].startswith("| Category | Value |"))
            self.assertLessEqual(len(chunk["content"]), 200)
            metadata = chunk["provenance"]["sourceBlocks"][0]["metadata"]
            self.assertTrue(metadata["chartDataAvailable"])
            self.assertFalse(metadata["ocrApplied"])
            self.assertEqual(metadata["assetSha256"], "a" * 64)

    def test_explicit_heading_boundary_toggle_is_honored(self) -> None:
        parsed = ParsedDocument(text="First\nSecond", provider="fixture", blocks=(ParsedBlock("paragraph", "First", heading_path=("One",)), ParsedBlock("paragraph", "Second", heading_path=("Two",))))
        enabled = _chunk_document(parsed, document_id="doc", base_id="base", chunking_config=DEFAULT_CHUNKING_CONFIG)
        disabled = _chunk_document(parsed, document_id="doc", base_id="base", chunking_config={**DEFAULT_CHUNKING_CONFIG, "respectHeadings": False})
        self.assertEqual(2, len(enabled))
        self.assertEqual(1, len(disabled))
        self.assertEqual(2, len(disabled[0]["provenance"]["sourceBlocks"]))

    def test_explicit_qa_and_laws_profiles_keep_their_text_template_rules(self) -> None:
        text = "Question: Why?\nAnswer: Evidence.\n\n第一条 范围\n第二条 义务"
        plain = ParsedDocument(text=text, provider="fixture")
        structured = ParsedDocument(text=text, provider="fixture", blocks=(ParsedBlock("paragraph", text, heading_path=("Context",)),))
        for strategy in ("qa", "laws"):
            config = {**DEFAULT_CHUNKING_CONFIG, "strategy": strategy}
            self.assertEqual(_chunk_document(plain, document_id="doc", base_id="base", chunking_config=config), _chunk_document(structured, document_id="doc", base_id="base", chunking_config=config))

    def test_trimmed_html_table_preserves_caption_when_split(self) -> None:
        rows = [["Key", "Value"]] + [[f"sample-{i}", str(i)] for i in range(25)]
        html = "<table>" + "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows) + "</table>\n"
        parsed = ParsedDocument(text=html, provider="fixture", blocks=(ParsedBlock("table", "Crucial caption\n" + html, metadata={"rows": rows, "tableHtml": html}),))
        chunks = _chunk_document(parsed, document_id="doc", base_id="base", chunking_config={**DEFAULT_CHUNKING_CONFIG, "size": 200})
        self.assertIn("Crucial caption", "\n".join(row["content"] for row in chunks))

    def test_truncated_heading_labels_do_not_merge_distinct_sections(self) -> None:
        blocks = tuple(ParsedBlock("paragraph", text, heading_path=("H" * 150 + text,)) for text in ("AAA", "BBB"))
        parsed = ParsedDocument(text="AAA BBB", provider="fixture", blocks=blocks)
        chunks = _chunk_document(parsed, document_id="doc", base_id="base", chunking_config=DEFAULT_CHUNKING_CONFIG)
        self.assertEqual(["AAA", "BBB"], [row["content"] for row in chunks])

    def test_numbered_bibliography_entries_never_become_discarded_headings(self) -> None:
        parsed = ParsedDocument(text="Abstract\nSummary.\n\nReferences\n1. Smith, Paper A, 2024.\n2. Jones, Paper B, 2025.", provider="builtin")
        chunks = _chunk_document(parsed, document_id="doc", base_id="base", chunking_config={**DEFAULT_CHUNKING_CONFIG, "strategy": "paper"})
        refs = [item for item in chunks if item["provenance"]["kind"] == "reference"]
        self.assertTrue(refs)
        self.assertIn("1. Smith, Paper A, 2024.", "\n".join(item["content"] for item in refs))
        self.assertIn("2. Jones, Paper B, 2025.", "\n".join(item["content"] for item in refs))

    def test_source_metadata_and_block_count_remain_bounded(self) -> None:
        blocks = tuple(ParsedBlock("paragraph", f"unit {i}", heading_path=("H" * 10000,), metadata={"sheetName": "S" * 20000}) for i in range(100))
        chunks = _chunk_document(ParsedDocument(text="source", provider="fixture", blocks=blocks), document_id="doc", base_id="base", chunking_config=DEFAULT_CHUNKING_CONFIG)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertLessEqual(len(json.dumps(chunk["provenance"], ensure_ascii=False).encode()), 8192)
            self.assertLessEqual(len(chunk["provenance"]["sourceBlocks"]), 16)
        self.assertEqual(100, sum(len(item["provenance"]["sourceBlocks"]) for item in chunks))

    def test_prose_overlap_is_applied_inside_long_structured_blocks(self) -> None:
        body = "".join(str(i % 10) for i in range(520))
        parsed = ParsedDocument(text=body, provider="fixture", blocks=(ParsedBlock("paragraph", body, page=1),))
        chunks = _chunk_document(parsed, document_id="doc", base_id="base", chunking_config={**DEFAULT_CHUNKING_CONFIG, "size": 200, "overlap": 37})
        self.assertEqual(chunks[0]["content"][-37:], chunks[1]["content"][:37])
        no_overlap = _chunk_document(parsed, document_id="doc", base_id="base", chunking_config={**DEFAULT_CHUNKING_CONFIG, "size": 200, "overlap": 0})
        self.assertNotEqual(chunks[1]["content"], no_overlap[1]["content"])

    def test_blocks_keep_real_page_geometry_and_atomic_evidence(self) -> None:
        parsed = ParsedDocument(
            text="# Results\nMeasured flux\n$$E=mc^2$$\n| Name | Value |\n|---|---|\n| flux | 12 |",
            provider="fixture-layout",
            blocks=(
                ParsedBlock("paragraph", "Measured flux", page=5, bbox=(10, 20, 100, 40), heading_path=("Results",), metadata={"coordinateSystem": "normalized-1000"}),
                ParsedBlock("formula", "$$E=mc^2$$", page=5, heading_path=("Results",)),
                ParsedBlock("table", "| Name | Value |\n|---|---|\n| flux | 12 |", page=6, heading_path=("Results",)),
            ),
        )
        chunks = _chunk_document(parsed, document_id="doc", base_id="base", chunking_config=DEFAULT_CHUNKING_CONFIG)
        self.assertEqual([5, 5, 6], [row["page"] for row in chunks])
        self.assertEqual("$$E=mc^2$$", chunks[1]["content"])
        self.assertEqual([10, 20, 100, 40], chunks[0]["provenance"]["sourceBlocks"][0]["bbox"])
        self.assertEqual("table", chunks[2]["provenance"]["kind"])

    def test_paper_sections_and_references_are_retained_without_invented_pages(self) -> None:
        parsed = ParsedDocument(text="Abstract\nEvidence summary.\n\n1 Introduction\nPrior work.\n\n2 Results\nMeasured flux.\n\nReferences\n[1] Example, 2024.", provider="builtin")
        chunks = _chunk_document(parsed, document_id="doc", base_id="base", chunking_config={**DEFAULT_CHUNKING_CONFIG, "strategy": "paper"})
        self.assertEqual(["Abstract", "1 Introduction", "2 Results", "References"], [row["heading"] for row in chunks])
        self.assertTrue(all(row["page"] is None for row in chunks))
        self.assertIn("[1] Example, 2024.", chunks[-1]["content"])
        self.assertEqual("reference", chunks[-1]["provenance"]["kind"])

    def test_oversized_table_keeps_rows_and_repeats_header_with_bounded_chunks(self) -> None:
        table = "| Item | Value |\n|---|---|\n" + "\n".join(f"| sample-{index} | {index} |" for index in range(40))
        parsed = ParsedDocument(text=table, provider="fixture", blocks=(ParsedBlock("table", table, page=2),))
        chunks = _chunk_document(parsed, document_id="doc", base_id="base", chunking_config={**DEFAULT_CHUNKING_CONFIG, "size": 200, "overlap": 20})
        self.assertGreater(len(chunks), 1)
        for row in chunks:
            self.assertLessEqual(len(row["content"]), 200)
            self.assertTrue(row["content"].startswith("| Item | Value |\n|---|---|"))
            self.assertEqual(2, row["page"])
        for index in range(40):
            self.assertEqual(1, sum(f"| sample-{index} | {index} |" in row["content"] for row in chunks))

    def test_geometry_survives_import_search_open_and_restart(self) -> None:
        class Parser:
            def parse(self, _path: Path, *, mode: str = "auto") -> ParsedDocument:
                return ParsedDocument(text="Measured flux is 12.", provider="fixture", blocks=(ParsedBlock("paragraph", "Measured flux is 12.", page=5, bbox=(1, 2, 3, 4), metadata={"coordinateSystem": "normalized-1000"}),))

        with tempfile.TemporaryDirectory() as root:
            config = KnowledgeLibraryConfig(Path(root) / "Knowledge")
            service = KnowledgeLibraryService(config, parser_router=Parser())
            base = service.create_base("Papers", retrieval_config={"mode": "lexical"})
            source = Path(root) / "paper.md"
            source.write_text("Measured flux is 12.", encoding="utf-8")
            document = service.import_document(base["id"], source)
            service.close()
            service = KnowledgeLibraryService(config)
            self.addCleanup(service.close)
            result = service.search("flux", base_ids=(base["id"],), mode="lexical")
            hit = result["hits"][0]
            self.assertEqual(5, hit["citation"]["page"])
            self.assertEqual([1, 2, 3, 4], hit["citation"]["sourceBlocks"][0]["bbox"])
            detail = service.document_detail(base["id"], document["documentId"])
            self.assertEqual("paragraph", detail["chunks"]["items"][0]["provenance"]["kind"])
            opened = service.open(hit["chunkId"])
            self.assertEqual([1, 2, 3, 4], opened["chunks"][0]["provenance"]["sourceBlocks"][0]["bbox"])
            preview = service.preview_chunking(base["id"], document["documentId"], {"strategy": "paper"})
            self.assertEqual(5, preview["items"][0]["page"])
            self.assertEqual([1, 2, 3, 4], preview["items"][0]["provenance"]["sourceBlocks"][0]["bbox"])
            snapshot = service.store.export_search_snapshot(base["id"])
            imported = KnowledgeLibraryService(KnowledgeLibraryConfig(Path(root) / "Imported"))
            self.addCleanup(imported.close)
            imported.store.import_search_snapshot(snapshot)
            self.assertEqual(hit["citation"], imported.search("flux", base_ids=(base["id"],), mode="lexical")["hits"][0]["citation"])

    def test_paper_auto_chooses_configured_layout_engine_without_changing_explicit_builtin(self) -> None:
        calls: list[str] = []

        class Parser:
            def parse(self, _path: Path, *, mode: str = "auto") -> ParsedDocument:
                calls.append(mode)
                return ParsedDocument(text="Abstract\nExample paper.", provider="fixture")

        with tempfile.TemporaryDirectory() as root:
            service = KnowledgeLibraryService(KnowledgeLibraryConfig(Path(root) / "Knowledge", mineru_enabled=True), parser_router=Parser())
            self.addCleanup(service.close)
            base = service.create_base("Paper routing", chunking_config={"strategy": "paper"})
            path = Path(root) / "paper.pdf"
            path.write_bytes(b"%PDF fixture")
            document = service.import_document(base["id"], path)
            self.assertEqual("ready", document["status"])
            self.assertEqual(["mineru"], calls)
            service.retry_document(document["documentId"], parser_mode="builtin")
            self.assertEqual(["mineru", "builtin"], calls)

    def test_pre_structure_database_is_migrated_without_losing_old_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config = KnowledgeLibraryConfig(Path(root) / "Knowledge")
            service = KnowledgeLibraryService(config)
            base = service.create_base("Legacy")
            source = Path(root) / "legacy.md"
            source.write_text("Legacy evidence remains searchable", encoding="utf-8")
            service.import_document(base["id"], source)
            with service.store.connection() as connection:
                connection.execute("ALTER TABLE knowledge_chunks DROP COLUMN provenance_json")
            service.close()
            restored = KnowledgeLibraryService(config)
            self.addCleanup(restored.close)
            hits = restored.search("evidence", base_ids=(base["id"],), mode="lexical")["hits"]
            self.assertEqual(1, len(hits))
            self.assertNotIn("sourceBlocks", hits[0]["citation"])

    def test_rebuild_reapplies_auto_routing_after_selecting_paper_profile(self) -> None:
        calls = []

        class Parser:
            def parse(self, _path: Path, *, mode: str = "auto") -> ParsedDocument:
                calls.append(mode)
                return ParsedDocument(text="Abstract\nA paper body.", provider="builtin" if mode == "auto" else "mineru_local_http")

        with tempfile.TemporaryDirectory() as root:
            service = KnowledgeLibraryService(KnowledgeLibraryConfig(Path(root) / "Knowledge", mineru_enabled=True), parser_router=Parser())
            self.addCleanup(service.close)
            base = service.create_base("Existing papers")
            source = Path(root) / "paper.pdf"
            source.write_bytes(b"%PDF fixture")
            document = service.import_document(base["id"], source)
            service.update_base(base["id"], chunking_config={"strategy": "paper"})
            preview = service.reindex_preview(base["id"])
            service.rebuild_base(base["id"], preview_token=preview["previewToken"], expected_revision=preview["configRevision"], confirm_text="REBUILD")
            self.assertEqual(["auto", "mineru"], calls)
            service.retry_document(document["documentId"], parser_mode="builtin")
            service.retry_document(document["documentId"])
            self.assertEqual(["auto", "mineru", "builtin", "builtin"], calls)


if __name__ == "__main__":
    unittest.main()
