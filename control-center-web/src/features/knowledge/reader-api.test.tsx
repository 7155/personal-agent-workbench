import type { ReactNode } from 'react';
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import { useKnowledgeDocumentDetail } from './api';
import { chunkAssets, chunkTables, localMarkdownAsset } from './structured-reading';
afterEach(cleanup);
it('carries persisted block identities through transport normalization without shifting blank cells', async () => {
  const hash = 'a'.repeat(64);
  const transport = new MockControlTransport({ routes: { 'knowledgeBases.document.get': {
    document: { documentId: 'paper', name: 'Tools.pdf', pageCount: 12 },
    chunks: { items: [{ chunkId: 'hit', content: 'Agent Runtime', provenance: { kind: 'table', sourceBlocks: [{ id: 'source', order: 7, metadata: { assetSha256: hash, imagePath: 'images/figure.png', ocrApplied: false, chartDataAvailable: false } }] } }], total: 1 },
    tables: [{ tableId: 'table-block-7', sourceBlockOrders: [7], columns: ['Name', 'Absent', 'Value'], rows: [['Agent Runtime', '', '36']], truncated: true, totalRowCount: 900 }],
    assets: [{ assetId: hash, name: 'figure.png', sha256: hash, mimeType: 'image/png', readPath: `/api/knowledge-bases/base/documents/paper/assets/${hash}`, pages: [5, 9], sourcePaths: ['images/figure.png'], locations: [{ sourceBlockOrder: 7, page: 5, caption: 'Tools' }], locationCount: 2, locationsTruncated: true }],
  } } });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <ControlTransportProvider transport={transport}><QueryClientProvider client={client}>{children}</QueryClientProvider></ControlTransportProvider>;
  const { result, unmount } = renderHook(() => useKnowledgeDocumentDetail('base', 'paper'), { wrapper });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  const detail = result.current.data!;
  expect(detail.tables[0].rows[0]).toEqual(['Agent Runtime', '', '36']);
  expect(detail.chunks[0].provenance?.sourceBlocks[0]).toMatchObject({ id: 'source', order: 7, assetSha256: hash, ocrApplied: false, chartDataAvailable: false });
  expect(chunkTables(detail.chunks[0], detail.tables)).toEqual(detail.tables);
  expect(chunkAssets(detail.chunks[0], detail.assets)).toEqual(detail.assets);
  expect(localMarkdownAsset('images/figure.png', detail.assets)).toEqual(detail.assets[0]);
  expect(detail.assets[0]).toMatchObject({ page: null, pages: [5, 9], locationCount: 2, locationsTruncated: true });
  expect(detail.tables[0]).toMatchObject({ truncated: true, totalRowCount: 900 });
  unmount(); client.clear();
});
