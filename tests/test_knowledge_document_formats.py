from __future__ import annotations

import tempfile
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from rag_ime.knowledge_library.models import DocumentParseError
from rag_ime.knowledge_library.parsers import BuiltinDocumentParser
from rag_ime.knowledge_library import legacy_office


class DocumentFormatTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def package(self, suffix, entries):
        path = self.root / ("fixture" + suffix)
        with zipfile.ZipFile(path, "w") as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
        return path

    def epub(self, extra=None, spine='second first'):
        entries = {
            'mimetype': 'application/epub+zip',
            'META-INF/container.xml': '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>',
            'OPS/book.opf': '<package xmlns:dc="urn:dc"><metadata><dc:title>Source Book</dc:title></metadata><manifest>'
                '<item id="first" href="first.xhtml" media-type="application/xhtml+xml"/>'
                '<item id="second" href="second.xhtml" media-type="application/xhtml+xml"/>'
                '<item id="img" href="images/figure.png" media-type="image/png"/>'
                '</manifest><spine>' + ''.join(f'<itemref idref="{item}"/>' for item in spine.split()) + '</spine></package>',
            'OPS/first.xhtml': '<html><body><h1>First</h1><p>After second.</p></body></html>',
            'OPS/second.xhtml': '<html><head><title>Ignore head</title></head><body><h1>Second</h1><p>hyp<b>othesis</b> &amp; evidence</p><img src="images/figure.png" alt="Trial diagram"/><p>After figure</p></body></html>',
            'OPS/images/figure.png': b'png-bytes',
        }
        entries.update(extra or {})
        return self.package('.epub', entries)

    def test_epub_preserves_spine_headings_inline_words_and_image_assets_without_pages(self):
        parsed = BuiltinDocumentParser().parse(self.epub())
        self.assertEqual('Source Book', parsed.title)
        self.assertEqual(['Second', 'First'], [b.text for b in parsed.blocks if b.kind == 'heading'])
        self.assertIn('hypothesis & evidence', parsed.text)
        self.assertNotIn('Ignore head', parsed.text)
        image = next(b for b in parsed.blocks if b.kind == 'image')
        self.assertEqual(('Second',), image.heading_path)
        self.assertEqual('Trial diagram', image.text)
        self.assertEqual(parsed.assets[0].sha256, image.metadata['assetSha256'])
        self.assertTrue(all(b.page is None for b in parsed.blocks))
        self.assertNotIn('pageCount', parsed.metadata)

    def test_epub_missing_spine_part_and_escaping_href_fail_explicitly(self):
        with self.assertRaises(DocumentParseError):
            BuiltinDocumentParser().parse(self.epub(spine='missing'))
        with self.assertRaises(DocumentParseError) as error:
            BuiltinDocumentParser().parse(self.epub({'META-INF/container.xml': '<container><rootfile full-path="../../outside.opf"/></container>'}))
        self.assertEqual('unsafe_archive', error.exception.code)

    def test_epub_encryption_and_external_images_have_explicit_boundaries(self):
        with self.assertRaises(DocumentParseError) as error:
            BuiltinDocumentParser().parse(self.epub({'META-INF/encryption.xml': '<encryption><CipherReference URI="OPS/second.xhtml"/></encryption>'}))
        self.assertEqual('epub_encrypted', error.exception.code)
        parsed = BuiltinDocumentParser().parse(self.epub({'OPS/second.xhtml': '<html><body><p>Body</p><img src="https://example.invalid/figure.png" alt="External diagram"/></body></html>'}))
        image = next(block for block in parsed.blocks if block.kind == 'image')
        self.assertTrue(image.metadata['externalImageSkipped'])
        self.assertNotIn('assetSha256', image.metadata)

    def test_duplicate_package_members_are_rejected_before_parsing(self):
        path = self.root / 'duplicate.epub'
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('META-INF/container.xml', '<container/>')
                archive.writestr('META-INF/container.xml', '<different/>')
        with self.assertRaises(DocumentParseError) as error:
            BuiltinDocumentParser().parse(path)
        self.assertEqual('unsafe_archive', error.exception.code)

    def test_docx_image_relationship_is_a_local_asset_not_ocr(self):
        parsed = BuiltinDocumentParser().parse(self.package('.docx', {
            'word/document.xml': '<w:document xmlns:w="urn:w" xmlns:a="urn:a" xmlns:r="urn:r" xmlns:wp="urn:wp"><w:body><w:p><w:r><w:t>Before</w:t></w:r></w:p><w:p><w:r><w:drawing><wp:docPr descr="Microscope view"/><a:blip r:embed="img1"/></w:drawing></w:r></w:p><w:p><w:r><w:t>After</w:t></w:r></w:p></w:body></w:document>',
            'word/_rels/document.xml.rels': '<Relationships><Relationship Id="img1" Type="http://schemas/image" Target="media/image1.png"/></Relationships>',
            'word/media/image1.png': b'embedded-png',
        }))
        self.assertEqual(['paragraph', 'image', 'paragraph'], [b.kind for b in parsed.blocks])
        image = parsed.blocks[1]
        self.assertEqual('Microscope view', image.text)
        self.assertEqual('embedded-alt-text', image.metadata['textSource'])
        self.assertFalse(image.metadata['ocrApplied'])
        self.assertEqual(b'embedded-png', parsed.assets[0].data)

    def test_pptx_chart_cached_data_is_readable_and_bound_to_slide(self):
        parsed = BuiltinDocumentParser().parse(self.package('.pptx', {
            'ppt/slides/slide1.xml': '<p:sld xmlns:p="urn:p" xmlns:c="urn:c" xmlns:r="urn:r"><p:cSld><p:spTree><p:graphicFrame><c:chart r:id="chart1"/></p:graphicFrame></p:spTree></p:cSld></p:sld>',
            'ppt/slides/_rels/slide1.xml.rels': '<Relationships><Relationship Id="chart1" Type="http://schemas/chart" Target="../charts/chart1.xml"/></Relationships>',
            'ppt/charts/chart1.xml': '<c:chartSpace xmlns:c="urn:c"><c:chart><c:barChart><c:ser><c:tx><c:v>Revenue</c:v></c:tx><c:cat><c:strRef><c:strCache><c:pt idx="0"><c:v>North</c:v></c:pt></c:strCache></c:strRef></c:cat><c:val><c:numRef><c:numCache><c:pt idx="0"><c:v>42</c:v></c:pt></c:numCache></c:numRef></c:val></c:ser></c:barChart></c:chart></c:chartSpace>',
        }))
        chart = next(b for b in parsed.blocks if b.metadata.get('textSource') == 'chart-cached-data')
        self.assertEqual(1, chart.page)
        self.assertIn('Revenue', chart.text)
        self.assertIn('North', chart.text)
        self.assertIn('42', chart.text)
        self.assertFalse(chart.metadata['ocrApplied'])

    def test_old_office_reports_missing_conversion_dependency(self):
        # Patch discovery rather than modifying PATH or installing a converter.
        with patch('shutil.which', return_value=None), patch('pathlib.Path.is_file', return_value=False):
            for suffix in ('.doc', '.xls', '.ppt'):
                source = self.root / ('legacy' + suffix)
                source.write_bytes(b'legacy-fixture')
                with self.subTest(suffix=suffix), self.assertRaises(DocumentParseError) as error:
                    BuiltinDocumentParser().parse(source)
                self.assertEqual('office_converter_unavailable', error.exception.code)

    def test_xlsx_follows_drawing_relationships_without_inventing_pages(self):
        parsed = BuiltinDocumentParser().parse(self.package('.xlsx', {
            'xl/worksheets/sheet1.xml': '<worksheet xmlns:r="urn:r"><drawing r:id="draw"/></worksheet>',
            'xl/worksheets/_rels/sheet1.xml.rels': '<Relationships><Relationship Id="draw" Type="http://schemas/drawing" Target="../drawings/drawing1.xml"/></Relationships>',
            'xl/drawings/drawing1.xml': '<drawing xmlns:a="urn:a" xmlns:r="urn:r"><pic><cNvPr descr="Survey plot"/><a:blip r:embed="image"/></pic></drawing>',
            'xl/drawings/_rels/drawing1.xml.rels': '<Relationships><Relationship Id="image" Type="http://schemas/image" Target="../media/plot.png"/></Relationships>',
            'xl/media/plot.png': b'plot-image',
        }))
        image = next(b for b in parsed.blocks if b.kind == 'image')
        self.assertIsNone(image.page)
        self.assertEqual(('sheet1',), image.heading_path)
        self.assertEqual('Survey plot', image.text)
        self.assertEqual(b'plot-image', parsed.assets[0].data)

    def test_chart_without_cache_has_explicit_unknown_state(self):
        parsed = BuiltinDocumentParser().parse(self.package('.pptx', {
            'ppt/slides/slide1.xml': '<slide xmlns:r="urn:r"><spTree><graphicFrame><chart r:id="chart"/></graphicFrame></spTree></slide>',
            'ppt/slides/_rels/slide1.xml.rels': '<Relationships><Relationship Id="chart" Type="http://schemas/chart" Target="../charts/chart1.xml"/></Relationships>',
            'ppt/charts/chart1.xml': '<chartSpace><chart><ser><val><numRef><f>Sheet1!A1:A3</f></numRef></val></ser></chart></chartSpace>',
        }))
        chart = next(b for b in parsed.blocks if b.kind == 'chart')
        self.assertFalse(chart.metadata['chartDataAvailable'])
        self.assertIn('cached data unavailable', chart.text)

    def test_legacy_conversion_isolated_profile_preserves_input_and_provenance(self):
        source = self.root / 'original.doc'
        source.write_bytes(b'original binary')
        commands = []
        temporary_paths = []

        def run(command, directory, *, timeout, output=None):
            commands.append(command)
            temporary_paths.append(directory)
            profile = (directory / 'profile/user/registrymodifications.xcu').read_text()
            self.assertIn('<value>3</value>', profile)
            if output is None:
                return 'LibreOffice Test 1.0'
            self.assertEqual(b'original binary', (directory / 'input.doc').read_bytes())
            with zipfile.ZipFile(output, 'w') as archive:
                archive.writestr('word/document.xml', '<document><body><p><r><t>Converted evidence</t></r></p></body></document>')
            return 'converted'

        with patch.object(legacy_office, 'find_office_converter', return_value='/local/soffice'), patch.object(legacy_office, '_run_converter', side_effect=run):
            parsed = BuiltinDocumentParser().parse(source)
        self.assertEqual(b'original binary', source.read_bytes())
        self.assertEqual('LibreOffice Test 1.0', parsed.metadata['converterVersion'])
        self.assertEqual('.doc', parsed.metadata['originalFormat'])
        self.assertEqual('.docx', parsed.metadata['convertedFormat'])
        self.assertIn('Converted evidence', parsed.text)
        self.assertTrue(all(not path.exists() for path in temporary_paths))
        self.assertTrue(all(any(arg.startswith('-env:UserInstallation=file:') for arg in command) for command in commands))

    def test_converter_timeout_and_output_limit_are_explicit(self):
        with self.assertRaises(DocumentParseError) as error:
            legacy_office._run_converter([sys.executable, '-c', 'import time; time.sleep(2)'], self.root, timeout=0.01)
        self.assertEqual('office_conversion_timeout', error.exception.code)
        output = self.root / 'oversize.docx'
        command = [sys.executable, '-c', 'import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b"x"*64)', str(output)]
        with patch.object(legacy_office, 'MAX_CONVERSION_BYTES', 32), self.assertRaises(DocumentParseError) as error:
            legacy_office._run_converter(command, self.root, timeout=1, output=output)
        self.assertEqual('office_conversion_limit', error.exception.code)

    def test_converter_success_status_without_output_is_not_parse_success(self):
        source = self.root / 'empty-output.doc'
        source.write_bytes(b'legacy')
        with patch.object(legacy_office, 'find_office_converter', return_value='/local/soffice'), patch.object(legacy_office, '_run_converter', return_value='LibreOffice test'):
            with self.assertRaises(DocumentParseError) as error:
                BuiltinDocumentParser().parse(source)
        self.assertEqual('office_conversion_failed', error.exception.code)


if __name__ == '__main__':
    unittest.main()
