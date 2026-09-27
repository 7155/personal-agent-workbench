from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from rag_ime.knowledge_library.models import KnowledgeLibraryConfig, ParsedAsset, ParsedBlock, ParsedDocument
from rag_ime.knowledge_library.service import KnowledgeLibraryService
from rag_ime.knowledge_library.reader_artifacts import enrich_assets


class ReaderStructureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def detail(self, parsed):
        class Parser:
            def parse(self, _path, **_kwargs):
                return parsed
        service = KnowledgeLibraryService(KnowledgeLibraryConfig(self.root / 'Knowledge'), parser_router=Parser())
        self.addCleanup(service.close)
        base = service.create_base('Reader', retrieval_config={'mode': 'lexical'})
        source = self.root / 'source.md'
        source.write_text('fixture')
        document = service.import_document(base['id'], source)
        self.assertEqual('ready', document['status'])
        return service.document_detail(base['id'], document['documentId'])

    def test_identical_tables_on_same_page_keep_distinct_source_orders(self):
        text = '| Name | Value |\n| --- | --- |\n| A | 42 |'
        blocks = tuple(ParsedBlock('table', text, page=3, bbox=(0, y, 100, y+10),
                                   metadata={'rows': [['A', '42']], 'caption': 'Results'}) for y in (10, 80))
        detail = self.detail(ParsedDocument(text=text+'\n\n'+text, provider='fixture', blocks=blocks))
        tables = detail['tables']
        self.assertEqual([[0], [1]], [table['sourceBlockOrders'] for table in tables])
        self.assertEqual(2, len({table['tableId'] for table in tables}))
        self.assertEqual([3, 3], [table['page'] for table in tables])
        self.assertEqual([['A', '42']], tables[0]['rows'])
        self.assertEqual('Results', tables[0]['title'])
        orders = {source['order'] for chunk in detail['chunks']['items'] for source in chunk['provenance']['sourceBlocks']}
        self.assertEqual({0, 1}, orders)

    def test_unknown_chart_and_unknown_page_remain_explicit(self):
        block = ParsedBlock('chart', '[Embedded chart; cached data unavailable]',
                            metadata={'chartDataAvailable': False, 'caption': 'Uncached chart'})
        detail = self.detail(ParsedDocument(text=block.text, provider='fixture', blocks=(block,), metadata={'pageCount': 8}))
        chart = detail['tables'][0]
        self.assertEqual('chart', chart['kind'])
        self.assertFalse(chart['dataAvailable'])
        self.assertIsNone(chart['page'])
        self.assertEqual([], chart['rows'])

    def test_large_structural_tables_have_explicit_global_budgets(self):
        rows = [['界' * 800] + [str(i)] * 39 for i in range(350)]
        blocks = [{'kind': 'table', 'text': 'Large table', 'page': None, 'metadata': {'rows': rows}},
                  *({'kind': 'table', 'text': '| A |\n| --- |\n| x |', 'page': 1} for _ in range(40))]
        service = KnowledgeLibraryService(KnowledgeLibraryConfig(self.root / 'Limits'))
        self.addCleanup(service.close)
        tables = service._artifact_tables({'artifact_path': '', 'metadata_json': json.dumps({'parsedBlocks': blocks})})
        self.assertTrue(tables)
        self.assertTrue(tables[0]['truncated'])
        self.assertIn('rows', tables[0]['truncationReasons'])
        self.assertEqual(350, tables[0]['totalRowCount'])
        self.assertLessEqual(sum(len(t['rows']) for t in tables), 800)
        self.assertLessEqual(sum(len(row) for t in tables for row in t['rows']), 16_000)
        self.assertLessEqual(len(json.dumps(tables, ensure_ascii=False).encode()), 512 * 1024)
        self.assertTrue(any('tables' in t['truncationReasons'] for t in tables))

    def test_asset_hash_keeps_multiple_locations_and_recorded_source_paths(self):
        data = b'picture'; digest = hashlib.sha256(data).hexdigest()
        asset = ParsedAsset('figure.png', 'image/png', digest, data)
        blocks = tuple(ParsedBlock('image', caption, page=page,
                                   metadata={'assetSha256': digest, 'imagePath': path, 'caption': caption})
                       for page, caption, path in ((2, 'Top view', 'images/figure.png'), (5, 'Side view', 'other/figure.png'), (None, 'Unknown page', 'images/figure.png')))
        detail = self.detail(ParsedDocument(text='Images', provider='fixture', blocks=blocks, assets=(asset,)))
        result = detail['assets'][0]
        self.assertIsNone(result['page'])
        self.assertEqual([2, 5], result['pages'])
        self.assertIsNone(result['caption'])
        self.assertEqual([0, 1, 2], [loc['sourceBlockOrder'] for loc in result['locations']])
        self.assertEqual(['images/figure.png', 'other/figure.png'], result['sourcePaths'])
        self.assertEqual(3, result['locationCount'])

    def test_ambiguous_basename_does_not_bind_an_asset(self):
        assets = tuple(ParsedAsset('same.png', 'image/png', hashlib.sha256(data).hexdigest(), data) for data in (b'one', b'two'))
        block = ParsedBlock('image', 'Caption', page=7, metadata={'imagePath': 'images/same.png', 'caption': 'Caption'})
        detail = self.detail(ParsedDocument(text='Images', provider='fixture', blocks=(block,), assets=assets))
        self.assertTrue(all(asset['locations'] == [] for asset in detail['assets']))
        self.assertTrue(all(asset['page'] is None for asset in detail['assets']))

    def test_legacy_text_tables_still_work_without_fabricated_block_identity(self):
        detail = self.detail(ParsedDocument(text='| A |\n| --- |\n| Legacy |', provider='fixture'))
        table = detail['tables'][0]
        self.assertEqual([['Legacy']], table['rows'])
        self.assertEqual([], table['sourceBlockOrders'])
        self.assertIsNone(table['page'])

    def test_asset_locations_and_captions_are_bounded_with_explicit_truncation(self):
        assets = [{'assetId': 'a'*64, 'name': 'image.png'}]
        blocks = [{'kind': 'image', 'text': 'image', 'page': index+1,
                   'metadata': {'assetSha256': 'a'*64, 'caption': '界'*1000, 'imagePath': f'images/{index}.png'}} for index in range(100)]
        asset = enrich_assets(assets, blocks)[0]
        self.assertEqual(100, asset['locationCount'])
        self.assertTrue(asset['locationsTruncated'])
        self.assertLessEqual(len(asset['locations']), 32)
        self.assertLessEqual(len(asset['sourcePaths']), 32)
        self.assertLessEqual(len(asset['locations'][0]['caption'].encode()), 512)
        self.assertIsNone(asset['page'])

    def test_caption_truncation_does_not_merge_distinct_captions(self):
        blocks = [{'kind': 'image', 'text': 'image', 'page': 2,
                   'metadata': {'assetSha256': 'a'*64, 'caption': 'x'*600 + suffix}} for suffix in ('A', 'B')]
        asset = enrich_assets([{'assetId': 'a'*64, 'name': 'image.png'}], blocks)[0]
        self.assertIsNone(asset['caption'])
        self.assertTrue(asset['locationsTruncated'])
        self.assertEqual(2, asset['page'])

    def test_legacy_large_table_reports_real_row_truncation(self):
        table = '| A |\n| --- |\n' + '\n'.join(f'| {i} |' for i in range(240))
        detail = self.detail(ParsedDocument(text=table, provider='legacy'))
        self.assertTrue(detail['tables'][0]['truncated'])
        self.assertIn('rows', detail['tables'][0]['truncationReasons'])
        self.assertEqual(240, detail['tables'][0]['totalRowCount'])

    def test_malformed_old_structure_falls_back_to_text_table(self):
        config = KnowledgeLibraryConfig(self.root / 'Old')
        service = KnowledgeLibraryService(config)
        self.addCleanup(service.close)
        artifact = config.artifacts_dir / 'legacy.md'
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text('| A |\n| --- |\n| Old |')
        tables = service._artifact_tables({'artifact_path': str(artifact), 'metadata_json': '{"parsedBlocks":[{},"invalid",{"kind":"table","text":7}]}'})
        self.assertEqual([['Old']], tables[0]['rows'])
        self.assertEqual([], tables[0]['sourceBlockOrders'])

    def test_html_table_larger_than_scan_window_is_not_silently_lost(self):
        from rag_ime.knowledge_library.service import _extract_table_artifacts, _MAX_HTML_TABLE_SCAN_CHARS
        html = '<table><tr><td>' + 'x' * (_MAX_HTML_TABLE_SCAN_CHARS + 10) + '</td></tr></table>'
        tables = _extract_table_artifacts(html)
        self.assertEqual(1, len(tables))
        self.assertTrue(tables[0]['truncated'])
        self.assertIn('scan', tables[0]['truncationReasons'])
        self.assertIsNone(tables[0]['totalRowCount'])


if __name__ == '__main__':
    unittest.main()
