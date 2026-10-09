import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { TooltipProvider } from '@/components/primitives';
import { MockControlTransport } from '@/test/mock-transport';
import { DocumentSource, KnowledgeDocumentViewer } from './document-workspace';
import type { KnowledgeDocumentDetail, KnowledgeSearchHit } from './api';
import type { KnowledgeAssetPayload } from '@/platform/transport';
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

function mediaDetail(kind: 'audio' | 'frame' = 'audio'): KnowledgeDocumentDetail {
  const source = detail(); const audio = kind === 'audio'; const hash = 'a'.repeat(64);
  Object.assign(source.document, { name: 'public-clip.mp4', mimeType: 'video/mp4', sha256: 'b'.repeat(64), metadata: {
    nativeMediaEmbeddings: true, durationSeconds: 12, mediaUnitCount: 3,
    videoFramesTruncated: true, audioSegmentsTruncated: false, transcriptionApplied: false,
  } });
  source.document.sourceReadPath = '/api/knowledge-bases/base/documents/paper/source';
  source.document.pageCount = 0; source.pages = []; source.tables = []; source.contentWindow = [];
  source.chunks[0] = { ...source.chunks[0], page: null, content: audio ? '[audio-segment: 0–2s; no transcription]' : '[video-frame: 10–10s; no transcription]',
    provenance: Object.assign({ kind: audio ? 'audio' : 'image', parser: 'builtin', split: false, headingPath: [], sourceBlocks: [Object.assign({ order: 7, page: null, bbox: null, coordinateSystem: '', assetSha256: hash }, {
      kind: audio ? 'audio' : 'image', sourcePart: audio ? 'audio-segment' : 'video-frame', startSeconds: audio ? 0 : 10, endSeconds: audio ? 2 : 10,
      timestampKind: audio ? 'segment-offset' as const : 'seek-offset' as const, transcriptionApplied: false,
    })] }, { modality: audio ? 'audio' as const : 'image' as const, startSeconds: audio ? 0 : 10, endSeconds: audio ? 2 : 10 }),
  };
  source.assets = [{ id: hash, sha256: hash, name: audio ? 'audio-0000.wav' : 'image-0001.png', mimeType: audio ? 'audio/wav' : 'image/png', byteSize: 4,
    readPath: `/api/knowledge-bases/base/documents/paper/assets/${hash}`, page: null, caption: '' }];
  return source;
}

it('keeps partial-media warnings visible and opens the full media explanation from the keyboard without reading assets', async () => {
  const user = userEvent.setup();
  const transport = new MockControlTransport();
  const read = vi.spyOn(transport, 'readKnowledgeAsset');
  show(mediaDetail(), false, transport);
  const summary = screen.getByText('媒体说明').closest('summary')!;
  expect(summary).toHaveAttribute('aria-expanded', 'false');
  expect(screen.getByText('源文件时长 00:12 · 3 个采样单元')).toBeVisible();
  expect(screen.getByText(/采样画面仅覆盖部分视频/)).toBeVisible();
  expect(screen.queryByText('采样画面不代表连续视频理解；时间标记为采样偏移。')).not.toBeInTheDocument();
  summary.focus();
  await user.keyboard('{Enter}');
  expect(summary).toHaveAttribute('aria-expanded', 'true');
  expect(screen.getByText('媒体按音频片段或视频画面检索，未生成转写文本。')).toBeVisible();
  expect(screen.getByText('采样画面不代表连续视频理解；时间标记为采样偏移。')).toBeVisible();
  await user.keyboard(' ');
  expect(summary).toHaveAttribute('aria-expanded', 'false');
  expect(summary).toHaveFocus();
  expect(screen.getByText(/采样画面仅覆盖部分视频/)).toBeVisible();
  expect(read).not.toHaveBeenCalled();
});

it('makes native media readable in the default view and reads a bound audio asset only on demand', async () => {
  const source = mediaDetail();
  const transport = new MockControlTransport({ knowledgeAsset: ({ kbId, fileId, assetId }) => ({ kbId, fileId, assetId, sha256: assetId, mimeType: 'audio/wav', byteSize: 4, blob: new Blob(['wave'], { type: 'audio/wav' }) }) });
  const read = vi.spyOn(transport, 'readKnowledgeAsset'); const view = show(source, false, transport);
  expect(screen.getByText('音频片段 · 00:00–00:02')).toBeVisible();
  expect(screen.getByText('此片段按原音频提取，未生成转写文本。')).toBeVisible();
  expect(screen.getByText(/采样画面仅覆盖部分视频/)).toBeVisible();
  expect(read).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '查看音频片段：00:00–00:02' }));
  const player = await screen.findByLabelText('音频片段：00:00–00:02');
  expect(player.tagName).toBe('AUDIO'); expect(player).toHaveAttribute('controls'); expect(player).toHaveAttribute('preload', 'none');
  expect(player).not.toHaveAttribute('autoplay'); expect(screen.queryByRole('img')).not.toBeInTheDocument();
  expect(read).toHaveBeenCalledOnce();
  expect(read.mock.calls[0][0]).toMatchObject({ kbId: 'base', fileId: 'paper', assetId: 'a'.repeat(64) });
  expect(read.mock.calls[0][0]).not.toHaveProperty('path');
  view.unmount(); expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:structured-image');
});

it('labels video frames as sampling offsets while retaining the exact original hit in disclosure', async () => {
  const source = mediaDetail('frame'); show(source);
  expect(screen.getByText('采样画面 · 00:10（采样偏移）')).toBeVisible();
  expect(screen.getByRole('button', { name: '查看采样画面：00:10（采样偏移）' })).toBeVisible();
  expect(screen.getByText('此画面按视频采样提取，不代表连续视频理解。')).toBeVisible();
  fireEvent.click(screen.getByText('检索片段原文', { selector: 'summary' }));
  expect(screen.getByText('[video-frame: 10–10s; no transcription]', { selector: 'mark' })).toBeVisible();
});

it('keeps the original audio retry button focused and guarded while its real read is held', async () => {
  const user = userEvent.setup(); const source = mediaDetail();
  let resolve!: (payload: KnowledgeAssetPayload) => void;
  const held = new Promise<KnowledgeAssetPayload>((done) => { resolve = done; });
  const reader = vi.fn().mockRejectedValueOnce(new Error('public read failure')).mockImplementationOnce(() => held);
  const transport = new MockControlTransport({ knowledgeAsset: reader }); show(source, true, transport);
  await user.click(screen.getByRole('button', { name: '查看音频片段：00:00–00:02' }));
  const retry = await screen.findByRole('button', { name: '重新读取音频' });
  retry.focus(); await user.keyboard('{Enter}');
  await waitFor(() => expect(retry).toHaveAttribute('aria-busy', 'true'));
  expect(screen.getByRole('button', { name: '重新读取音频' })).toBe(retry); expect(retry).toHaveFocus(); expect(retry).not.toBeDisabled();
  expect(screen.getByText('正在重新读取音频…')).toBeVisible();
  await user.click(retry); await user.keyboard('{Enter} '); expect(reader).toHaveBeenCalledTimes(2);
  await act(async () => resolve({ kbId: 'base', fileId: 'paper', assetId: 'a'.repeat(64), sha256: 'a'.repeat(64), mimeType: 'audio/wav', byteSize: 4, blob: new Blob(['wave'], { type: 'audio/wav' }) }));
  expect(await screen.findByLabelText('音频片段：00:00–00:02')).toBeVisible();
  expect(retry).toHaveFocus(); expect(retry).not.toHaveAttribute('aria-busy', 'true');
});

it('aborts a collapsed audio read and ignores a late binary result', async () => {
  const source = mediaDetail(); let resolve!: (payload: KnowledgeAssetPayload) => void;
  const transport = new MockControlTransport({ knowledgeAsset: () => new Promise((done) => { resolve = done; }) });
  show(source, true, transport); const toggle = screen.getByRole('button', { name: '查看音频片段：00:00–00:02' });
  fireEvent.click(toggle); await waitFor(() => expect(transport.knowledgeAssetCalls).toHaveLength(1));
  const signal = transport.knowledgeAssetCalls[0].signal!; fireEvent.click(toggle);
  expect(signal.aborted).toBe(true);
  await act(async () => resolve({ kbId: 'base', fileId: 'paper', assetId: 'a'.repeat(64), sha256: 'a'.repeat(64), mimeType: 'audio/wav', byteSize: 4, blob: new Blob(['wave'], { type: 'audio/wav' }) }));
  expect(URL.createObjectURL).not.toHaveBeenCalled(); expect(screen.queryByLabelText('音频片段：00:00–00:02')).not.toBeInTheDocument();
  expect(toggle).toHaveAttribute('aria-expanded', 'false');
});

it('keeps media source opens bound to the original source and does not pretend that time seeking exists', async () => {
  const source = mediaDetail('frame');
  const hit = { id: 'frame-hit', documentId: 'paper', page: null, provenance: source.chunks[0].provenance } as KnowledgeSearchHit;
  const transport = new MockControlTransport({ knowledgeDocumentSource: ({ kbId, fileId }) => ({ kbId, fileId, sha256: 'b'.repeat(64), mimeType: 'video/mp4', byteSize: 4, blob: new Blob(['clip'], { type: 'video/mp4' }) }) });
  const view = render(<DocumentSource detail={source} focusHit={hit} transport={transport} />);
  const open = await screen.findByRole('link', { name: '打开源文件' });
  expect(open).toHaveAttribute('href', 'blob:structured-image');
  expect(screen.getByText(/命中位置：00:10（采样偏移）/)).toBeVisible();
  expect(screen.getByText(/尚不支持自动跳转到媒体时间/)).toBeVisible();
  expect(transport.knowledgeDocumentSourceCalls[0]).toMatchObject({ kbId: 'base', fileId: 'paper' });
  expect(transport.knowledgeDocumentSourceCalls[0]).not.toHaveProperty('path');
  expect(screen.queryByRole('button', { name: '跳转' })).not.toBeInTheDocument();
  view.unmount(); expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:structured-image');
});

it.each([{ kbId: 'other' }, { fileId: 'other' }, { sha256: 'c'.repeat(64) }])('does not expose media from a source receipt with a changed binding %j', async (mismatch) => {
  const source = mediaDetail();
  const transport = new MockControlTransport({ knowledgeDocumentSource: ({ kbId, fileId }) => ({ kbId, fileId, sha256: 'b'.repeat(64), mimeType: 'video/mp4', byteSize: 4, blob: new Blob(['clip'], { type: 'video/mp4' }), ...mismatch }) });
  render(<DocumentSource detail={source} transport={transport} />);
  expect(await screen.findByText('源文件暂不可用')).toBeVisible();
  expect(screen.queryByRole('link', { name: '打开源文件' })).not.toBeInTheDocument();
  expect(URL.createObjectURL).not.toHaveBeenCalled();
});

it('shows recorded sampling positions in the media artifact gallery and handles a player decode failure truthfully', async () => {
  const user = userEvent.setup(); const source = mediaDetail('frame'); const audio = mediaDetail(); const hash = 'c'.repeat(64);
  audio.chunks[0].id = 'audio-hit'; audio.chunks[0].ordinal = 1;
  audio.chunks[0].provenance!.sourceBlocks[0].assetSha256 = hash;
  audio.assets[0] = { ...audio.assets[0], id: hash, sha256: hash, readPath: `/api/knowledge-bases/base/documents/paper/assets/${hash}` };
  source.chunks.push(audio.chunks[0]); source.assets.push(audio.assets[0]); source.chunkTotal = 2;
  const transport = new MockControlTransport({ knowledgeAsset: ({ kbId, fileId, assetId }) => {
    const mimeType = assetId === hash ? 'audio/wav' : 'image/png';
    return { kbId, fileId, assetId, sha256: assetId, mimeType, byteSize: 4, blob: new Blob(['data'], { type: mimeType }) };
  } });
  show(source, false, transport); await user.click(screen.getByRole('tab', { name: '解析产物' }));
  expect(await screen.findByText('已读采样位置：00:10（采样偏移）')).toBeVisible();
  expect(screen.getByText('已读片段位置：00:00–00:02')).toBeVisible();
  expect(transport.knowledgeAssetCalls.some((call) => call.assetId === hash)).toBe(false);
  await user.click(screen.getByRole('button', { name: '查看音频片段：audio-0000.wav' }));
  const player = await screen.findByLabelText('音频片段：audio-0000.wav'); fireEvent.error(player);
  expect(screen.getByText('此浏览器无法播放此音频，可下载片段核对。')).toBeVisible();
  expect(screen.getByRole('link', { name: '下载音频片段' })).toHaveAttribute('download', 'audio-0000.wav');
  expect(player).not.toHaveAttribute('autoplay');
});

it('rereads the original media source when its known original hash changes and revokes the old Blob', async () => {
  const source = mediaDetail(); let count = 0;
  const transport = new MockControlTransport({ knowledgeDocumentSource: ({ kbId, fileId }) => ({ kbId, fileId, sha256: (++count === 1 ? 'b' : 'c').repeat(64), mimeType: 'video/mp4', byteSize: 4, blob: new Blob(['clip'], { type: 'video/mp4' }) }) });
  vi.mocked(URL.createObjectURL).mockReturnValueOnce('blob:old-media').mockReturnValueOnce('blob:new-media');
  const view = render(<DocumentSource detail={source} transport={transport} />);
  expect(await screen.findByRole('link', { name: '打开源文件' })).toHaveAttribute('href', 'blob:old-media');
  const changed = { ...source, document: { ...source.document, sha256: 'c'.repeat(64) } };
  view.rerender(<DocumentSource detail={changed} transport={transport} />);
  await waitFor(() => expect(screen.getByRole('link', { name: '打开源文件' })).toHaveAttribute('href', 'blob:new-media'));
  expect(transport.knowledgeDocumentSourceCalls).toHaveLength(2);
  expect(transport.knowledgeDocumentSourceCalls[0].signal?.aborted).toBe(true);
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:old-media');
});
