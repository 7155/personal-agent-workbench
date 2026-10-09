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

it('preserves native media metadata, modality and zero-based time positions through the original detail query', async () => {
  const hash = 'a'.repeat(64);
  const metadata = { nativeMediaEmbeddings: true, durationSeconds: 12, mediaUnitCount: 3, audioSegmentsTruncated: false, videoFramesTruncated: true, transcriptionApplied: false, parserNote: 'public fixture' };
  const transport = new MockControlTransport({ routes: { 'knowledgeBases.document.get': {
    document: { documentId: 'media', name: 'clip.mp4', sha256: 'b'.repeat(64), mimeType: 'video/mp4', metadata },
    chunks: { items: [
      { chunkId: 'audio', provenance: { kind: 'audio', modality: 'audio', startSeconds: 0, endSeconds: 2, sourceBlocks: [{ id: 'audio-0', order: 0, kind: 'audio', metadata: { assetSha256: hash, sourcePart: 'audio-segment', startSeconds: 0, endSeconds: 2, timestampKind: 'segment-offset', transcriptionApplied: false } }] } },
      { chunkId: 'frame', provenance: { kind: 'image', modality: 'image', startSeconds: 10, endSeconds: 10, sourceBlocks: [{ id: 'frame-1', order: 1, kind: 'image', metadata: { sourcePart: 'video-frame', startSeconds: 10, endSeconds: 10, timestampKind: 'seek-offset', transcriptionApplied: false } }] } },
    ], total: 2 },
    assets: [{ assetId: hash, sha256: hash, mimeType: 'audio/wav', readPath: `/api/knowledge-bases/base/documents/media/assets/${hash}`, locations: [{ sourceBlockOrder: 0, page: null, caption: '' }] }],
  } } });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <ControlTransportProvider transport={transport}><QueryClientProvider client={client}>{children}</QueryClientProvider></ControlTransportProvider>;
  const { result, unmount } = renderHook(() => useKnowledgeDocumentDetail('base', 'media'), { wrapper });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  const detail = result.current.data!;
  expect(detail.document).toMatchObject({ id: 'media', baseId: 'base', sha256: 'b'.repeat(64), metadata });
  expect(detail.chunks[0].provenance).toMatchObject({ modality: 'audio', startSeconds: 0, endSeconds: 2, sourceBlocks: [{ kind: 'audio', startSeconds: 0, endSeconds: 2, timestampKind: 'segment-offset', transcriptionApplied: false }] });
  expect(detail.chunks[1].provenance).toMatchObject({ modality: 'image', startSeconds: 10, endSeconds: 10, sourceBlocks: [{ timestampKind: 'seek-offset', startSeconds: 10, endSeconds: 10 }] });
  expect(chunkAssets(detail.chunks[0], detail.assets)).toEqual(detail.assets);
  expect(transport.requests[0].request).toMatchObject({ pathId: 'knowledgeBases.document.get', params: { kbId: 'base', fileId: 'media' } });
  unmount(); client.clear();
});

it.each([[-1, 2], [2, 1], [0, Number.POSITIVE_INFINITY], [Number.NaN, 2], ['0', 2], [0, undefined]])('does not turn invalid media range %s–%s into a valid zero position', async (startSeconds, endSeconds) => {
  const transport = new MockControlTransport({ routes: { 'knowledgeBases.document.get': {
    document: { documentId: 'media' }, chunks: [{ chunkId: 'bad', provenance: { kind: 'audio', modality: 'unknown', startSeconds, endSeconds,
      sourceBlocks: [{ order: 0, metadata: { startSeconds, endSeconds, timestampKind: 'guessed-frame' } }] } }],
  } } });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <ControlTransportProvider transport={transport}><QueryClientProvider client={client}>{children}</QueryClientProvider></ControlTransportProvider>;
  const { result, unmount } = renderHook(() => useKnowledgeDocumentDetail('base', 'media'), { wrapper });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data?.chunks[0].provenance).toMatchObject({ modality: null, startSeconds: null, endSeconds: null, sourceBlocks: [{ startSeconds: null, endSeconds: null, timestampKind: null }] });
  unmount(); client.clear();
});
