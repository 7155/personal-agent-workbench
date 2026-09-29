from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from rag_ime.embeddings import HashingEmbeddingProvider
from rag_ime.knowledge_library.dense import SqliteDenseIndex
from rag_ime.knowledge_library.models import KnowledgeLibraryConfig
from rag_ime.knowledge_library.parsers import MinerULocalParser, ParserRouter
from rag_ime.knowledge_library.service import KnowledgeLibraryService


def _zip_bytes(entries: dict[str, str | bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return stream.getvalue()


class KnowledgeParserPipelineTests(unittest.TestCase):
    """Real parser/store/retrieval paths; only the MinerU HTTP boundary is stubbed."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = KnowledgeLibraryConfig(self.root / "Knowledge", chunk_chars=400, chunk_overlap_chars=20)
        self.network = mock.patch("urllib.request.urlopen", side_effect=AssertionError("pipeline fixtures must stay offline"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.service = self._service(self.config)

    def _service(self, config: KnowledgeLibraryConfig, router: ParserRouter | None = None) -> KnowledgeLibraryService:
        config.root_dir.mkdir(parents=True, exist_ok=True)
        dense = SqliteDenseIndex(config.database_path, HashingEmbeddingProvider())
        service = KnowledgeLibraryService(config, parser_router=router, dense_index=dense)
        self.addCleanup(service.close)
        return service

    def _import(self, suffix: str, entries: dict[str, str | bytes]):
        source = self.root / f"source{suffix}"
        source.write_bytes(_zip_bytes(entries))
        base = self.service.create_base("Parser fixture", retrieval_config={"mode": "lexical", "graphEnabled": False})
        document = self.service.import_document(base["id"], source)
        self.assertEqual("ready", document["status"], document.get("error"))
        self.assertEqual("builtin", document["parserProvider"])
        return base, document

    def _search_open_preview(self, base, document, query):
        result = self.service.search(query, base_ids=(base["id"],), mode="lexical")
        self.assertTrue(result["hits"], result)
        hit = result["hits"][0]
        self.assertEqual(document["documentId"], hit["documentId"])
        opened = self.service.open(hit["chunkId"], before=0, after=0)["chunks"][0]
        self.assertEqual(hit["citation"]["sourceBlocks"], opened["provenance"]["sourceBlocks"])
        before_document = dict(self.service.store.get_document(document["documentId"]))
        before_chunks = self.service.open_document(document["documentId"])
        with mock.patch.object(self.service.parsers, "parse", side_effect=AssertionError("preview must use stored parsed blocks")):
            preview = self.service.preview_chunking(base["id"], document["documentId"], {"strategy": "paper", "size": 200, "overlap": 0}, limit=30)
        self.assertFalse(preview["truncated"])
        self.assertEqual(before_document, dict(self.service.store.get_document(document["documentId"])))
        self.assertEqual(before_chunks, self.service.open_document(document["documentId"]))
        return hit, opened, preview

    def test_docx_runs_heading_and_table_survive_import_search_and_rechunk_preview(self) -> None:
        base, document = self._import(".docx", {
            "word/document.xml": '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
              <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Methods</w:t></w:r></w:p>
              <w:p><w:r><w:t>hyp</w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t>othesis</w:t></w:r>
              <w:r><w:t xml:space="preserve"> is supported.</w:t></w:r></w:p>
              <w:tbl><w:tr><w:tc><w:p><w:r><w:t>sampletoken</w:t></w:r></w:p></w:tc>
              <w:tc><w:p/></w:tc><w:tc><w:p><w:r><w:t>42</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
              </w:body></w:document>''',
        })
        hit, opened, preview = self._search_open_preview(base, document, "hypothesis")
        self.assertIn("hypothesis is supported.", hit["content"])
        self.assertEqual("Methods", opened["heading"])
        self.assertIsNone(hit["citation"]["page"])
        self.assertTrue(all(item["page"] is None for item in preview["items"]))
        table = next(item for item in preview["items"] if item["provenance"]["kind"] == "table")
        self.assertIn("| sampletoken |  | 42 |", table["content"])
        self.assertEqual("Methods", table["heading"])
        metadata = json.loads(self.service.store.get_document(document["documentId"])["metadata_json"])
        stored_table = next(block for block in metadata["parsedBlocks"] if block["kind"] == "table")
        self.assertEqual([["sampletoken", "", "42"]], stored_table["metadata"]["rows"])
        self.assertIn("<table>", stored_table["metadata"]["tableHtml"])

    def test_xlsx_blank_column_and_sheet_coordinates_survive_restart_and_preview(self) -> None:
        base, document = self._import(".xlsx", {
            "xl/workbook.xml": '<workbook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Field observations" sheetId="1" r:id="sheet-rel"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="sheet-rel" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/worksheets/sheet1.xml": '''<worksheet><sheetData><row r="7"><c r="A7" t="inlineStr"><is><t>sparsecell</t></is></c><c r="C7"><v>17</v></c></row>
              <row r="9"><c r="B9"><v>42</v></c></row></sheetData></worksheet>''',
        })
        self.service.close()
        self.service = self._service(self.config)
        hit, opened, preview = self._search_open_preview(base, document, "sparsecell")
        self.assertIn("| sparsecell |  | 17 |", opened["content"])
        self.assertIsNone(hit["citation"]["page"])
        source = hit["citation"]["sourceBlocks"][0]
        self.assertEqual("Field observations", source["metadata"]["sheetName"])
        self.assertEqual((7, 9), (source["metadata"]["rowStart"], source["metadata"]["rowEnd"]))
        table = next(item for item in preview["items"] if "sparsecell" in item["content"])
        self.assertEqual(source, table["provenance"]["sourceBlocks"][0])
        detail = self.service.document_detail(base["id"], document["documentId"])
        self.assertEqual([], detail["pages"])

    def test_pptx_notes_retain_declared_slide_page_through_the_pipeline(self) -> None:
        base, document = self._import(".pptx", {
            "ppt/presentation.xml": '<p:presentation xmlns:p="urn:p" xmlns:r="urn:r"><p:sldIdLst><p:sldId id="256" r:id="r2"/><p:sldId id="257" r:id="r10"/></p:sldIdLst></p:presentation>',
            "ppt/_rels/presentation.xml.rels": '<Relationships><Relationship Id="r2" Target="slides/slide2.xml"/><Relationship Id="r10" Target="slides/slide10.xml"/></Relationships>',
            "ppt/slides/slide2.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:p><a:r><a:t>First slide evidence</a:t></a:r></a:p></p:sld>',
            "ppt/slides/slide10.xml": '''<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><p:cSld><p:spTree><p:sp>
              <p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>Results</a:t></a:r></a:p></p:txBody>
              </p:sp></p:spTree></p:cSld></p:sld>''',
            "ppt/slides/_rels/slide10.xml.rels": '<Relationships><Relationship Id="notes" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" Target="../notesSlides/notesSlide3.xml"/></Relationships>',
            "ppt/notesSlides/notesSlide3.xml": '''<p:notes xmlns:p="urn:p" xmlns:a="urn:a"><p:cSld><p:spTree><p:sp>
              <p:nvSpPr><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>speakerunique explains the measured result.</a:t></a:r></a:p></p:txBody>
              </p:sp></p:spTree></p:cSld></p:notes>''',
        })
        hit, opened, preview = self._search_open_preview(base, document, "speakerunique")
        self.assertEqual(2, hit["citation"]["page"])
        self.assertEqual("speaker_notes", opened["provenance"]["kind"])
        self.assertEqual("Results", opened["heading"])
        notes = next(item for item in preview["items"] if "speakerunique" in item["content"])
        self.assertEqual(2, notes["page"])
        self.assertEqual("ppt/notesSlides/notesSlide3.xml", notes["provenance"]["sourceBlocks"][0]["metadata"]["sourcePart"])
        detail = self.service.document_detail(base["id"], document["documentId"])
        self.assertEqual([1, 2], [page["page"] for page in detail["pages"]])

    def test_mineru_archive_geometry_and_original_json_survive_storage_and_preview(self) -> None:
        raw = json.dumps([
            {"type": "text", "text": "Results", "text_level": 1, "page_idx": 4},
            {"type": "text", "text": "geometrytoken flux is 12.", "page_idx": 4, "bbox": [10, 20, 300, 80]},
            {"type": "table", "table_body": "<table><tr><td>flux</td><td>12</td></tr></table>", "page_idx": 5},
        ]).encode("utf-8")
        payload = _zip_bytes({
            "paper/full.md": "# Results\n\ngeometrytoken flux is 12.\n\n<table><tr><td>flux</td><td>12</td></tr></table>",
            "paper/paper_content_list.json": raw,
        })
        requests: list[str] = []

        class Response:
            status = 200

            def __init__(self, body: bytes, content_type: str):
                self.body = body
                self.headers = {"Content-Type": content_type}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, limit=-1):
                return self.body if limit < 0 else self.body[:limit]

        def fixture_http(request, **_kwargs):
            requests.append(request.full_url)
            if request.full_url.endswith("/openapi.json"):
                return Response(b'{"paths":{"/file_parse":{}},"info":{"version":"fixture"}}', "application/json")
            self.assertTrue(request.full_url.endswith("/file_parse"))
            self.assertEqual("POST", request.get_method())
            self.assertIn(b'name="return_content_list"\r\n\r\ntrue', request.data)
            return Response(payload, "application/zip")

        config = KnowledgeLibraryConfig(self.root / "MinerU fixture", mineru_enabled=True)
        router = ParserRouter(config, mineru=MinerULocalParser(urlopen=fixture_http))
        self.service = self._service(config, router)
        base = self.service.create_base("MinerU fixture", parser_mode="mineru", retrieval_config={"mode": "lexical", "graphEnabled": False})
        source = self.root / "paper.png"
        source.write_bytes(b"\x89PNG\r\n\x1a\nlocal fixture input; parser HTTP is stubbed")
        document = self.service.import_document(base["id"], source)
        self.assertEqual("ready", document["status"], document.get("error"))
        self.assertEqual(1, sum(url.endswith("/file_parse") for url in requests))
        parse_request_count = len(requests)
        self.service.close()
        self.service = self._service(config)
        hit, opened, preview = self._search_open_preview(base, document, "geometrytoken")
        self.assertEqual(5, hit["citation"]["page"])
        self.assertEqual("Results", opened["heading"])
        for source_block in (hit["citation"]["sourceBlocks"][0], opened["provenance"]["sourceBlocks"][0]):
            self.assertEqual([10, 20, 300, 80], source_block["bbox"])
            self.assertEqual("normalized-1000", source_block["metadata"]["coordinateSystem"])
        preview_body = next(item for item in preview["items"] if "geometrytoken" in item["content"])
        self.assertEqual(5, preview_body["page"])
        self.assertEqual([10, 20, 300, 80], preview_body["provenance"]["sourceBlocks"][0]["bbox"])
        asset = next(row for row in self.service.store.document_assets(document["documentId"]) if row["media_type"] == "application/json")
        self.assertEqual(raw, Path(asset["stored_path"]).read_bytes())
        self.assertEqual(hashlib.sha256(raw).hexdigest(), asset["sha256"])
        metadata = json.loads(self.service.store.get_document(document["documentId"])["metadata_json"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), metadata["structureSha256"])
        self.assertEqual(parse_request_count, len(requests), "restart/search/preview must not call the parser service")


if __name__ == "__main__":
    unittest.main()
