import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { TooltipProvider } from '@/components/primitives';
import { MockControlTransport } from '@/test/mock-transport';
import { KnowledgeDocumentViewer } from './document-workspace';
import type { KnowledgeDocumentDetail, KnowledgeSearchHit } from './api';
vi.mock('@/features/evidence-echo/EvidenceEchoUsage', () => ({ EvidenceEchoUsage: () => null }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
beforeEach(() => { vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:structured-image'); vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined); });
const raw = '<table><tr><th>Sample</th><th>Flux</th></tr><tr><td>Agent Runtime</td><td>36</td></tr></table>';
function detail(): KnowledgeDocumentDetail {
  return {
    document: { id: 'paper', baseId: 'base', name: 'Agent Runtime.pdf', mimeType: 'application/pdf', byteSize: 10, status: 'ready', stage: 'ready', progress: 1, error: '', chunkCount: 1, parser: 'mineru', updatedAtMs: 1, revision: 1, sha256: '', pageCount: 8, tokenCount: 0, parserVersion: '1', sourceReadPath: '/source', indexedConfigRevision: 1 },
    chunks: [{ id: 'table-hit', ordinal: 0, content: raw, page: 5, heading: 'Tools', lineStart: null, lineEnd: null, tokenCount: 1, provenance: { kind: 'table', parser: 'mineru', split: false, headingPath: ['Tools'], sourceBlocks: [{ order: 7, page: 5, bbox: null, coordinateSystem: '' }] } }],
    chunkTotal: 1, chunkHasMore: false, pages: [{ page: 5, chunkCount: 1 }], assets: [],
    tables: [{ id: 'table', title: 'Table 1', page: 5, columns: ['Sample', 'Flux'], rows: [['Agent Runtime', '36']], markdown: raw, sourceBlockOrders: [7], dataAvailable: true }],
    artifact: { available: true, format: 'markdown', mimeType: 'text/markdown', byteSize: 100, lineCount: 2, sha256: '' },
    contentWindow: [{ lineNumber: 1, content: '# Tools' }, { lineNumber: 2, content: raw }], contentLineTotal: 2, contentHasMore: false,
  };
}
function show(source: KnowledgeDocumentDetail, focus = true, transport = new MockControlTransport()) {
  const hit: KnowledgeSearchHit = { id: 'table-hit', documentId: 'paper', documentName: source.document.name, title: 'Tools', excerpt: 'Agent Runtime', score: 1, page: 5, heading: 'Tools', lineStart: null, lineEnd: null, diagnostics: {} as KnowledgeSearchHit['diagnostics'] };
  return render(<TooltipProvider><KnowledgeDocumentViewer detail={source} documents={[source.document]} selectedDocumentId="paper" focusHit={focus ? hit : null} transport={transport} error={null} loading={false} onRetry={vi.fn()} onBack={vi.fn()} backLabel="返回" onImportMaterials={vi.fn()} onSelectDocument={vi.fn()} hasMoreChunks={false} hasMoreContent={false} loadMoreChunksFailed={false} loadingMoreChunks={false} loadingMoreContent={false} onLoadMoreChunks={vi.fn()} onLoadMoreContent={vi.fn()} /></TooltipProvider>);
}
it('renders the correct table in a hit while keeping exact original source wording', () => {
  show(detail());
  expect(screen.getByRole('table', { name: 'Table 1' })).toBeVisible();
  expect(screen.getByRole('cell', { name: 'Agent Runtime' })).toBeVisible();
  expect(screen.queryByText('伙伴运行环境')).not.toBeInTheDocument();
  expect(screen.getByText('8 页')).toBeVisible();
  fireEvent.click(screen.getByText('检索片段原文', { selector: 'summary' }));
  expect(screen.getByText('Agent Runtime', { selector: 'mark' })).toBeVisible();
});
it('does not turn known chunk pages into an invented document page count', () => {
  const source = detail(); source.document.pageCount = 0;
  show(source);
  expect(screen.getByText('已定位 1 个页码 · 总页数未提供')).toBeVisible();
  expect(screen.queryByText('1 页')).not.toBeInTheDocument();
});
it('does not present a full source table as the exact text of a split hit', () => {
  const source = detail(); source.chunks[0].provenance!.split = true; source.tables[0].truncated = true; source.tables[0].totalRowCount = 900;
  show(source);
  expect(screen.getByText(/此命中是表格的一部分/)).toBeVisible();
  fireEvent.click(screen.getByText('查看来源表格', { selector: 'summary' }));
  expect(screen.getByText(/来源共 900 行/)).toBeVisible();
});
it('renders safe known HTML tables in the body without rewriting factual terminology', () => {
  show(detail(), false);
  expect(screen.getByRole('table')).toBeVisible();
  expect(screen.getByRole('heading', { name: 'Tools' })).toBeVisible();
  expect(screen.getByRole('cell', { name: 'Agent Runtime' })).toBeVisible();
});
it('opens only a linked local image on demand and labels the lack of OCR', async () => {
  const source = detail();
  source.chunks[0].provenance = { kind: 'image', parser: 'builtin', split: false, headingPath: [], sourceBlocks: [{ order: 7, page: null, bbox: null, coordinateSystem: '', assetSha256: 'a'.repeat(64), ocrApplied: false }] };
  source.tables = [];
  source.assets = [{ id: 'a'.repeat(64), sha256: 'a'.repeat(64), name: 'figure.png', mimeType: 'image/png', byteSize: 1, readPath: '/asset', page: null, caption: 'Measured flux' }];
  const transport = new MockControlTransport({ knowledgeAsset: async ({ kbId, fileId, assetId }) => ({ kbId, fileId, assetId, mimeType: 'image/png', byteSize: 1, sha256: assetId, blob: new Blob(['image'], { type: 'image/png' }) }) });
  const read = vi.spyOn(transport, 'readKnowledgeAsset');
  show(source, true, transport);
  expect(read).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '查看图片：Measured flux' }));
  expect(await screen.findByRole('img', { name: 'Measured flux' })).toHaveAttribute('src', 'blob:structured-image');
  expect(read).toHaveBeenCalledOnce();
  expect(screen.getByText(/未进行图片 OCR/)).toBeVisible();
});
it('keeps partial chart categories visible while distinguishing missing numeric caches from zero', () => {
  const source = detail();
  source.tables[0] = { ...source.tables[0], kind: 'chart', dataAvailable: false, columns: ['Series', 'Category', 'Value'], rows: [['Revenue', 'North', '']] };
  show(source);
  expect(screen.getByRole('cell', { name: 'North' })).toBeVisible();
  expect(screen.getByText(/数值缓存不完整，空白不代表零/)).toBeVisible();
});
