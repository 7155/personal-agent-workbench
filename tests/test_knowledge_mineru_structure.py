from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from rag_ime.knowledge_library.models import DocumentParseError, ParsedBlock, ParsedDocument
from rag_ime.knowledge_library.parsers import MinerULocalParser, ParserRouter, inspect_mineru_zip


def archive_bytes(entries: dict[str, bytes]) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return target.getvalue()


class MinerUStructureTests(unittest.TestCase):
    def parse(self, entries: dict[str, bytes]):
        payload = archive_bytes({"result/full.md": b"# Paper\n\nBody fallback", **entries})

        class Response:
            status = 200
            headers = {"Content-Type": "application/zip"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _limit=-1):
                return payload

        def urlopen(request, **_kwargs):
            self.request_body = request.data
            return Response()

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "paper.png"
            source.write_bytes(b"synthetic input; HTTP is stubbed")
            return MinerULocalParser(urlopen=urlopen).parse(source)

    def test_structured_units_preserve_order_geometry_and_raw_artifact(self):
        items = [
            {"type": "text", "text": "Methods", "text_level": 1, "page_idx": 4, "bbox": [10, 20, 800, 60]},
            {"type": "text", "text": "The body", "page_idx": 4},
            {"type": "table", "table_body": "<table><tr><td>42</td></tr></table>", "table_caption": ["Table 1"], "page_idx": 5},
            {"type": "image", "image_caption": ["Figure 1"], "img_path": "images/figure.png", "page_idx": 5},
            {"type": "equation", "text": "$$x^2$$", "page_idx": 5},
            {"type": "text", "text": "References", "text_level": 1},
            {"type": "reference", "text": "[1] Original source"},
        ]
        raw = json.dumps(items).encode()
        parsed = self.parse({"result/paper_content_list.json": raw})
        self.assertEqual(["heading", "paragraph", "table", "image", "equation", "heading", "reference"], [b.kind for b in parsed.blocks])
        self.assertEqual([5, 5, 6, 6, 6, None, None], [b.page for b in parsed.blocks])
        self.assertEqual((10.0, 20.0, 800.0, 60.0), parsed.blocks[0].bbox)
        self.assertEqual("normalized-1000", parsed.blocks[0].metadata["coordinateSystem"])
        self.assertEqual(("Methods",), parsed.blocks[1].heading_path)
        self.assertIn("Table 1", parsed.blocks[2].text)
        self.assertIn("<table>", parsed.blocks[2].text)
        self.assertEqual([["42"]], parsed.blocks[2].metadata["rows"])
        self.assertEqual("images/figure.png", parsed.blocks[3].metadata["imagePath"])
        self.assertEqual("$$x^2$$", parsed.blocks[4].text)
        self.assertEqual("[1] Original source", parsed.blocks[6].text)
        self.assertEqual("available", parsed.metadata["structureStatus"])
        artifact = next(a for a in parsed.assets if a.media_type == "application/json")
        self.assertEqual(raw, artifact.data)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), parsed.metadata["structureSha256"])
        self.assertIn(b'name="return_content_list"\r\n\r\ntrue\r\n', self.request_body)

    def test_markdown_only_reports_fallback_without_inventing_positions(self):
        parsed = self.parse({})
        self.assertEqual((), parsed.blocks)
        self.assertEqual("fallback", parsed.metadata["structureStatus"])
        self.assertEqual("content_list_missing", parsed.metadata["structureFallbackReason"])

    def test_unknown_schema_is_retained_with_explicit_fallback(self):
        raw = b'{"version": "future", "pages": []}'
        parsed = self.parse({"result/content_list.json": raw})
        self.assertEqual((), parsed.blocks)
        self.assertEqual("unsupported_content_list_schema", parsed.metadata["structureFallbackReason"])
        self.assertEqual(raw, parsed.assets[0].data)

    def test_bad_structural_json_and_geometry_do_not_silently_fallback(self):
        for raw in (b"[broken", b'[3]', b'[{"type":"text","text":"body","page_idx":-1}]', b'[{"type":"text","text":"body","bbox":[1,2,3]}]'):
            with self.subTest(raw=raw):
                with self.assertRaises(DocumentParseError) as error:
                    self.parse({"result/content_list.json": raw})
                self.assertEqual("mineru_invalid_structure", error.exception.code)

    def test_unknown_block_is_preserved_and_reported_as_partial(self):
        parsed = self.parse({"result/content_list.json": b'[{"type":"future_block","text":"Keep me"}]'})
        self.assertEqual("unknown", parsed.blocks[0].kind)
        self.assertEqual("Keep me", parsed.blocks[0].text)
        self.assertEqual("partial", parsed.metadata["structureStatus"])
        self.assertEqual(["future_block"], parsed.metadata["unsupportedBlockTypes"])

    def test_legacy_inspection_still_returns_three_values(self):
        result = inspect_mineru_zip(archive_bytes({"full.md": b"Body"}))
        self.assertEqual(3, len(result))

    def test_heading_ancestry_and_table_cells_retain_semantic_units(self):
        items = [
            {"type": "text", "text": "Methods", "text_level": 1},
            {"type": "text", "text": "Setup", "text_level": 2},
            {"type": "text", "text": "Body"},
            {"type": "table", "table_body": "<table><tr><th>A</th><th>B</th></tr><tr><td>x<br>y</td><td>&amp; 2</td></tr></table>"},
            {"type": "text", "text": "Results", "text_level": 1},
        ]
        parsed = self.parse({"result/content_list.json": json.dumps(items).encode()})
        self.assertEqual(("Methods", "Setup"), parsed.blocks[2].heading_path)
        self.assertEqual(("Results",), parsed.blocks[4].heading_path)
        self.assertEqual([["A", "B"], ["x\ny", "& 2"]], parsed.blocks[3].metadata["rows"])

    def test_ambiguous_content_lists_are_rejected(self):
        with self.assertRaises(DocumentParseError) as error:
            self.parse({"result/a_content_list.json": b"[]", "result/b_content_list.json": b"[]"})
        self.assertEqual("mineru_invalid_structure", error.exception.code)

    def test_merged_cells_expand_without_shifting_columns_and_limits_are_explicit(self):
        html = '<table><tr><th rowspan="2">Region</th><th colspan="2">Sales</th></tr><tr><th>2025</th><th>2026</th></tr></table>'
        items = [{"type": "table", "table_body": html},
                 {"type": "table", "table_body": '<table><tr><td colspan="999999">Too wide</td></tr></table>'}]
        parsed = self.parse({"result/content_list.json": json.dumps(items).encode()})
        self.assertEqual([["Region", "Sales", "Sales"], ["Region", "2025", "2026"]], parsed.blocks[0].metadata["rows"])
        self.assertEqual(html, parsed.blocks[0].metadata["tableHtml"])
        self.assertNotIn("rows", parsed.blocks[1].metadata)
        self.assertEqual("span_limit_or_invalid", parsed.blocks[1].metadata["tableRowsFallbackReason"])

    def test_router_rejects_malformed_typed_blocks_before_persistence(self):
        cases = [
            ("not a block",),
            (ParsedBlock(kind="paragraph", text=17),),
            (ParsedBlock(kind="paragraph", text="body", page=True),),
            (ParsedBlock(kind="paragraph", text="body", page=0),),
            (ParsedBlock(kind="paragraph", text="body", bbox=(0, 1, -1, 2)),),
            (ParsedBlock(kind="paragraph", text="body", bbox=(0, 1, 2, float("nan"))),),
            (ParsedBlock(kind="paragraph", text="body", heading_path=(12,)),),
            (ParsedBlock(kind="paragraph", text="body", metadata=None),),
        ]
        for blocks in cases:
            with self.subTest(blocks=blocks):
                with self.assertRaises(DocumentParseError) as error:
                    ParserRouter.validate_output(ParsedDocument(text="body", provider="fixture", blocks=blocks), Path("paper.pdf"))
                self.assertEqual("parser_invalid_result", error.exception.code)

    def test_router_accepts_unknown_geometry_and_normalizes_list_envelope(self):
        block = ParsedBlock(kind="paragraph", text="body")
        parsed = ParsedDocument(text="body", provider="fixture", blocks=[block])
        validated = ParserRouter.validate_output(parsed, Path("paper.pdf"))
        self.assertEqual((block,), validated.blocks)
        self.assertIsNone(validated.blocks[0].page)
        self.assertIsNone(validated.blocks[0].bbox)


if __name__ == "__main__":
    unittest.main()
