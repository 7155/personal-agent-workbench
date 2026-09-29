from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from rag_ime.knowledge_library.models import DocumentParseError
from rag_ime.knowledge_library.parsers import BuiltinDocumentParser, _extract_office_text


class OfficeStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def package(self, suffix: str, entries: dict[str, str]) -> Path:
        path = self.root / f"fixture{suffix}"
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        return path

    def test_docx_joins_formatting_runs_without_breaking_words(self) -> None:
        path = self.package(".docx", {"word/document.xml": '''
          <w:document xmlns:w="urn:w"><w:body>
          <w:p><w:r><w:t>hyp</w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t>othesis</w:t></w:r>
          <w:r><w:t xml:space="preserve"> is supported</w:t><w:tab/><w:t>by data</w:t><w:br/><w:t>Second line</w:t></w:r></w:p>
          <w:p><w:r><w:t>Next paragraph</w:t></w:r></w:p>
          </w:body></w:document>'''})
        parsed = BuiltinDocumentParser().parse(path)
        self.assertIn("hypothesis is supported\tby data\nSecond line", parsed.text)
        self.assertEqual(["paragraph", "paragraph"], [block.kind for block in parsed.blocks])
        self.assertEqual("Next paragraph", parsed.blocks[1].text)
        self.assertTrue(all(block.page is None for block in parsed.blocks))

    def test_docx_preserves_heading_table_and_body_order_without_inventing_pages(self) -> None:
        path = self.package(".docx", {
            "word/document.xml": '''<w:document xmlns:w="urn:w"><w:body>
              <w:p><w:pPr><w:pStyle w:val="StudyTitle"/></w:pPr><w:r><w:t>Methods</w:t></w:r></w:p>
              <w:p><w:r><w:t>Before table.</w:t></w:r></w:p>
              <w:tbl><w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/></w:tblGrid>
                <w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>Group &amp; sample</w:t></w:r></w:p></w:tc>
                <w:tc><w:p><w:r><w:t>Count</w:t></w:r></w:p></w:tc></w:tr>
                <w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc><w:tc><w:p/></w:tc>
                <w:tc><w:p><w:r><w:t>12</w:t></w:r></w:p></w:tc></w:tr>
              </w:tbl><w:p><w:r><w:t>After table.</w:t></w:r></w:p>
              </w:body></w:document>''',
            "word/styles.xml": '''<w:styles xmlns:w="urn:w"><w:style w:styleId="StudyTitle">
              <w:name w:val="Custom heading"/><w:pPr><w:outlineLvl w:val="1"/></w:pPr></w:style></w:styles>''',
            "word/header1.xml": '<w:hdr xmlns:w="urn:w"><w:p><w:r><w:t>Running title</w:t></w:r></w:p></w:hdr>',
        })
        parsed = BuiltinDocumentParser().parse(path)
        self.assertEqual(["heading", "paragraph", "table", "paragraph"], [b.kind for b in parsed.blocks[:4]])
        table = parsed.blocks[2]
        self.assertEqual(("Methods",), table.heading_path)
        self.assertEqual(["A", "", "12"], table.metadata["rows"][1])
        self.assertIn('colspan="2"', table.metadata["tableHtml"])
        self.assertIn("Group &amp; sample", table.metadata["tableHtml"])
        self.assertTrue(all(block.page is None for block in parsed.blocks))
        self.assertNotIn("\f", parsed.text)
        self.assertNotIn("pageCount", parsed.metadata)

    def test_xlsx_retains_sparse_columns_rich_strings_and_sheet_identity(self) -> None:
        path = self.package(".xlsx", {
            "xl/workbook.xml": '<workbook xmlns:r="urn:r"><sheets><sheet name="Experiment" r:id="r2"/><sheet name="Control" r:id="r1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="r2" Type="urn:worksheet" Target="worksheets/sheet2.xml"/><Relationship Id="r1" Type="urn:worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/sharedStrings.xml": '<sst><si><r><t>hyp</t></r><r><t>othesis</t></r><rPh><t>ignore phonetics</t></rPh></si></sst>',
            "xl/worksheets/sheet2.xml": '''<worksheet><sheetData><row r="3">
              <c r="A3" t="s"><v>0</v></c><c r="C3" t="inlineStr"><is><r><t>cell </t></r><r><t>C</t></r></is></c>
              </row><row r="5"><c r="B5"><f>SUM(A1:A2)</f><v>42</v></c></row></sheetData></worksheet>''',
            "xl/worksheets/sheet1.xml": '<worksheet><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>control</t></is></c></row></sheetData></worksheet>',
        })
        parsed = BuiltinDocumentParser().parse(path)
        tables = [block for block in parsed.blocks if block.kind == "table"]
        self.assertEqual(["Experiment", "Control"], [b.metadata["sheetName"] for b in tables])
        table = tables[0]
        self.assertEqual(["hypothesis", "", "cell C"], table.metadata["rows"][0])
        self.assertEqual([3, 5], table.metadata["rowNumbers"])
        formula_cell = next(cell for cell in table.metadata["cells"] if cell["reference"] == "B5")
        self.assertEqual({"row": 5, "column": 2, "formula": "SUM(A1:A2)"}, {key: formula_cell[key] for key in ("row", "column", "formula")})
        self.assertTrue(all(block.page is None for block in parsed.blocks))
        self.assertNotIn("\f", parsed.text)

    def test_xlsx_far_coordinates_do_not_expand_missing_rows_or_columns(self) -> None:
        path = self.package(".xlsx", {"xl/worksheets/sheet1.xml": '''<worksheet>
          <dimension ref="A1:XFD1048576"/><sheetData><row r="1048576">
          <c r="A1048576" t="inlineStr"><is><t>start</t></is></c>
          <c r="XFD1048576" t="inlineStr"><is><t>end</t></is></c>
          </row></sheetData></worksheet>'''})
        parsed = BuiltinDocumentParser().parse(path)
        table = next(block for block in parsed.blocks if block.kind == "table")
        self.assertEqual("coordinate-values", table.metadata["representation"])
        self.assertEqual([1, 16384], [cell["column"] for cell in table.metadata["cells"]])
        self.assertLess(len(parsed.text), 1000)
        self.assertIn("XFD1048576", parsed.text)

    def test_xlsx_rejects_out_of_range_cell_coordinates(self) -> None:
        path = self.package(".xlsx", {"xl/worksheets/sheet1.xml": '<worksheet><sheetData><row r="1"><c r="ZZZZZZZZZZZZ999999999999"><v>7</v></c></row></sheetData></worksheet>'})
        with self.assertRaises(DocumentParseError) as caught:
            BuiltinDocumentParser().parse(path)
        self.assertEqual("unsafe_archive", caught.exception.code)

    def test_pptx_follows_declared_slide_order_and_keeps_speaker_notes(self) -> None:
        path = self.package(".pptx", {
            "ppt/presentation.xml": '<p:presentation xmlns:p="urn:p" xmlns:r="urn:r"><p:sldIdLst><p:sldId id="256" r:id="r10"/><p:sldId id="257" r:id="r2"/></p:sldIdLst></p:presentation>',
            "ppt/_rels/presentation.xml.rels": '<Relationships><Relationship Id="r10" Target="slides/slide10.xml"/><Relationship Id="r2" Target="slides/slide2.xml"/></Relationships>',
            "ppt/slides/slide10.xml": '''<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><p:cSld><p:spTree><p:sp>
              <p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>First </a:t></a:r><a:r><a:t>slide</a:t></a:r></a:p></p:txBody>
              </p:sp></p:spTree></p:cSld></p:sld>''',
            "ppt/slides/slide2.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:p><a:r><a:t>Second slide</a:t></a:r></a:p></p:sld>',
            "ppt/slides/_rels/slide10.xml.rels": '<Relationships><Relationship Id="n1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" Target="../notesSlides/notesSlide8.xml"/></Relationships>',
            "ppt/notesSlides/notesSlide8.xml": '''<p:notes xmlns:p="urn:p" xmlns:a="urn:a"><p:cSld><p:spTree>
              <p:sp><p:nvSpPr><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>Speaker evidence</a:t></a:r></a:p></p:txBody></p:sp>
              <p:sp><p:nvSpPr><p:nvPr><p:ph type="sldNum"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>999</a:t></a:r></a:p></p:txBody></p:sp>
              </p:spTree></p:cSld></p:notes>''',
        })
        parsed = BuiltinDocumentParser().parse(path)
        self.assertLess(parsed.text.index("First slide"), parsed.text.index("Second slide"))
        self.assertIn("Speaker evidence", parsed.text)
        self.assertNotIn("999", parsed.text)
        self.assertEqual(2, parsed.metadata["pageCount"])
        notes = next(block for block in parsed.blocks if block.kind == "speaker_notes")
        self.assertEqual(1, notes.page)
        self.assertEqual(("First slide",), notes.heading_path)
        self.assertEqual(2, next(block for block in parsed.blocks if block.text == "Second slide").page)
        self.assertEqual(1, parsed.text.count("\f"))

    def test_pptx_table_keeps_cells_instead_of_flattening_them(self) -> None:
        path = self.package(".pptx", {"ppt/slides/slide1.xml": '''<p:sld xmlns:p="urn:p" xmlns:a="urn:a">
          <p:cSld><p:spTree><p:graphicFrame><a:graphic><a:graphicData><a:tbl>
          <a:tr><a:tc><a:txBody><a:p><a:r><a:t>hyp</a:t></a:r><a:r><a:t>othesis</a:t></a:r></a:p></a:txBody></a:tc>
          <a:tc><a:txBody><a:p><a:r><a:t>42</a:t></a:r></a:p></a:txBody></a:tc></a:tr>
          </a:tbl></a:graphicData></a:graphic></p:graphicFrame></p:spTree></p:cSld></p:sld>'''})
        parsed = BuiltinDocumentParser().parse(path)
        table = next(block for block in parsed.blocks if block.kind == "table")
        self.assertEqual([["hypothesis", "42"]], table.metadata["rows"])
        self.assertEqual(1, table.page)
        self.assertIn("<td", table.metadata["tableHtml"])

    def test_text_adapter_remains_compatible(self) -> None:
        path = self.package(".docx", {"word/document.xml": '<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>one</w:t></w:r><w:r><w:t>word</w:t></w:r></w:p></w:body></w:document>'})
        text, engine = _extract_office_text(path, suffix=".docx")
        self.assertEqual("oneword", text)
        self.assertEqual("docx-xml", engine)

    def test_empty_slide_keeps_the_next_slide_page_number(self) -> None:
        path = self.package(".pptx", {
            "ppt/slides/slide1.xml": '<p:sld xmlns:p="urn:p"/>',
            "ppt/slides/slide2.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:p><a:r><a:t>Page two</a:t></a:r></a:p></p:sld>',
        })
        parsed = BuiltinDocumentParser().parse(path)
        self.assertEqual(2, parsed.blocks[0].page)
        self.assertEqual(["", "Page two"], parsed.text.split("\f"))

    def test_many_sparse_rows_do_not_multiply_empty_grid_output(self) -> None:
        rows = ''.join(f'<row r="{row}"><c r="IV{row}"><v>{row}</v></c></row>' for row in range(1, 1001))
        path = self.package(".xlsx", {"xl/worksheets/sheet1.xml": f'<worksheet><sheetData>{rows}</sheetData></worksheet>'})
        parsed = BuiltinDocumentParser().parse(path)
        tables = [block for block in parsed.blocks if block.kind == "table"]
        self.assertTrue(all(block.metadata["representation"] == "coordinate-values" for block in tables))
        self.assertEqual(1000, sum(len(block.metadata["cells"]) for block in tables))
        self.assertLess(len(parsed.text), 50_000)

    def test_word_vertical_merges_preserve_the_origin_cell(self) -> None:
        path = self.package(".docx", {"word/document.xml": '''<w:document xmlns:w="urn:w"><w:body><w:tbl>
          <w:tr><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>Group A</w:t></w:r></w:p></w:tc>
          <w:tc><w:p><w:r><w:t>10</w:t></w:r></w:p></w:tc></w:tr>
          <w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc>
          <w:tc><w:p><w:r><w:t>20</w:t></w:r></w:p></w:tc></w:tr>
          </w:tbl></w:body></w:document>'''})
        table = BuiltinDocumentParser().parse(path).blocks[0]
        self.assertEqual(2, table.metadata["cells"][0]["rowSpan"])
        self.assertIn('rowspan="2"', table.metadata["tableHtml"])
        self.assertEqual([["Group A", "10"], ["", "20"]], table.metadata["rows"])

    def test_xlsx_keeps_merge_ranges_and_uncached_formulas(self) -> None:
        path = self.package(".xlsx", {"xl/worksheets/sheet1.xml": '''<worksheet><sheetData><row r="1">
          <c r="A1" t="inlineStr"><is><t>Merged</t></is></c></row><row r="2"><c r="A2"><f>SUM(A3:A4)</f></c></row>
          </sheetData><mergeCells><mergeCell ref="A1:C1"/></mergeCells></worksheet>'''})
        table = next(block for block in BuiltinDocumentParser().parse(path).blocks if block.kind == "table")
        self.assertEqual(["A1:C1"], table.metadata["mergeRanges"])
        self.assertEqual(["Merged", "", ""], table.metadata["rows"][0])
        self.assertEqual("=SUM(A3:A4)", table.metadata["cells"][1]["text"])
        self.assertFalse(table.metadata["cells"][1]["cachedValueAvailable"])

    def test_xml_entity_declarations_are_rejected_even_when_utf16_encoded(self) -> None:
        path = self.root / "entity.docx"
        xml = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE document [<!ENTITY x "expanded">]><document><p><t>&x;</t></p></document>'
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", xml.encode("utf-16"))
        with self.assertRaises(DocumentParseError) as caught:
            BuiltinDocumentParser().parse(path)
        self.assertEqual("unsafe_archive", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
