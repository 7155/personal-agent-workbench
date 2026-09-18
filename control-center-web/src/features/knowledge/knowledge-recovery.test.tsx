import type { ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport, type MockControlTransportOptions, type MockRouteHandler } from '@/test/mock-transport';
import { KnowledgeFeature } from './index';

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => <div>{data.map((item, index) => <div key={index}>{itemContent(index, item)}</div>)}</div>,
}));

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

describe('Knowledge reading recovery', () => {
  it('does not call a pending materials list an empty library', async () => {
    renderKnowledge(createTransport({ documents: () => new Promise(() => {}) }), '/knowledge?base=kb-reading&tab=materials');
    expect(await screen.findByRole('status', { name: '正在读取文件列表' })).toBeInTheDocument();
    expect(screen.queryByText('还没有资料')).not.toBeInTheDocument();
    expect(screen.queryByText('0 个文件 · 0 B · 0 个可检索')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '导入文件' })).toBeInTheDocument();
  });

  it('retries a failed materials list in place without suggesting that files are absent', async () => {
    let attempts = 0;
    const user = userEvent.setup();
    renderKnowledge(createTransport({ documents: () => {
      attempts += 1;
      if (attempts === 1) throw new Error('list unavailable');
      return { items: [sourceDocument] };
    } }), '/knowledge?base=kb-reading&tab=materials');
    await user.click(await screen.findByRole('button', { name: '重新读取文件列表' }));
    expect(await screen.findByRole('button', { name: '查看 source.md' })).toBeInTheDocument();
    expect(screen.queryByText('还没有资料')).not.toBeInTheDocument();
    expect(attempts).toBe(2);
  });

  it('reads an explicit source independently of a slow materials catalog', async () => {
    const transport = createTransport({ documents: () => new Promise(() => {}) });
    renderKnowledge(transport, '/knowledge?base=kb-reading&document=source-1&tab=viewer');
    expect(await screen.findByText('已经读取的来源正文。')).toBeInTheDocument();
    expect(screen.queryByText('先导入资料')).not.toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => request.pathId === 'knowledgeBases.document.get').every(({ request }) => request.params?.fileId === 'source-1')).toBe(true);
  });

  it('retries both the source paragraphs and正文 after a partial detail failure', async () => {
    let contentAttempts = 0;
    const user = userEvent.setup();
    const transport = createTransport({ detail: (request: ControlRequest) => {
      if (Number(request.query?.lineLimit) > 1 && ++contentAttempts === 1) throw new Error('content unavailable');
      return detail;
    } });
    renderKnowledge(transport, '/knowledge?base=kb-reading&document=source-1&tab=viewer');
    await user.click(await screen.findByRole('button', { name: '重新读取材料' }));
    await waitFor(() => expect(contentAttempts).toBe(2));
    expect(await screen.findByText('已经读取的来源正文。')).toBeInTheDocument();
    expect(screen.queryByText('暂时无法查看材料')).not.toBeInTheDocument();
  });

  it('stops automatic hit pagination after a failed page and retries the same offset only on request', async () => {
    const pageReads: number[] = [];
    let retryRequested = false;
    const user = userEvent.setup();
    const transport = createTransport({ detail: (request: ControlRequest) => {
      const offset = Number(request.query?.offset);
      if (Number(request.query?.limit) > 1 && offset > 0) {
        pageReads.push(offset);
        if (pageReads.length === 1) throw new Error('next page unavailable');
        // An accidental automatic replay remains pending, so the failure
        // cannot spin an unbounded request loop during the regression test.
        if (!retryRequested) return new Promise(() => {});
        return { ...detail, chunks: { items: [{ id: 'chunk-2', content: '命中页中的来源。' }], total: 2, hasMore: false } };
      }
      return { ...detail, chunks: { ...detail.chunks, hasMore: true } };
    } });
    renderKnowledge(transport, '/knowledge?base=kb-reading&tab=search');
    await user.type(await screen.findByRole('textbox', { name: '搜索知识库' }), '来源');
    await user.click(screen.getByRole('button', { name: '搜索' }));
    const hits = await screen.findByRole('listbox', { name: '检索结果' });
    await user.click(within(hits).getAllByRole('option')[1]!);
    await user.click(screen.getByRole('button', { name: '打开来源' }));

    await screen.findByText('暂时无法查看材料');
    expect(pageReads).toEqual([1]);
    expect(screen.getByText('第一段来源。')).toBeInTheDocument();
    const retry = screen.getByRole('button', { name: '重试加载命中段落' });
    expect(retry).toBeEnabled();
    expect(screen.getByText(/命中段落未能加载：来源段落 2/)).toBeInTheDocument();

    retryRequested = true;
    await user.click(retry);
    expect(await screen.findByText('命中页中的来源。')).toBeInTheDocument();
    expect(screen.getByText('第一段来源。')).toBeInTheDocument();
    expect(screen.getByText('已定位检索命中：来源段落 2')).toBeInTheDocument();
    expect(pageReads).toEqual([1, 1]);
    expect(transport.requests.filter(({ request }) => request.pathId === 'knowledgeBases.search')).toHaveLength(1);
  });

  it('retries a failed source in place without losing its document, hit or reading surface', async () => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:recovered-source');
    const user = userEvent.setup();
    let attempts = 0;
    const transport = createTransport({
      detail: { ...detail, document: binaryDocument },
      knowledgeDocumentSource: (input) => {
        if (++attempts === 1) throw new Error('source unavailable');
        return { ...input, mimeType: 'application/pdf', byteSize: 3, sha256: '', blob: new Blob(['pdf'], { type: 'application/pdf' }) };
      },
    });
    renderKnowledge(transport, '/knowledge?base=kb-reading&tab=search');
    await user.type(await screen.findByRole('textbox', { name: '搜索知识库' }), '来源');
    await user.click(screen.getByRole('button', { name: '搜索' }));
    await user.click(await screen.findByRole('button', { name: '打开来源' }));
    await screen.findByText('已定位检索命中：来源段落 1');
    await user.click(screen.getByRole('tab', { name: '源文件' }));
    const retry = await screen.findByRole('button', { name: '重新读取源文件' });
    const reader = document.querySelector('.knowledge-viewer') as HTMLElement;
    reader.scrollTop = 144;
    expect(attempts).toBe(1);

    await user.click(retry);
    expect(await screen.findByTitle('source.md 源文件')).toHaveAttribute('src', 'blob:recovered-source');
    expect(document.querySelector('.knowledge-viewer')).toBe(reader);
    expect(reader.scrollTop).toBe(144);
    expect(screen.getByRole('tab', { name: '源文件' })).toHaveAttribute('aria-selected', 'true');
    expect(transport.knowledgeDocumentSourceCalls.map(({ fileId }) => fileId)).toEqual(['source-1', 'source-1']);
    await user.click(screen.getByRole('tab', { name: '段落' }));
    expect(await screen.findByText('已定位检索命中：来源段落 1')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '返回搜索结果' }));
    expect(await screen.findByRole('textbox', { name: '搜索知识库' })).toHaveValue('来源');
    expect(transport.requests.filter(({ request }) => request.pathId === 'knowledgeBases.search')).toHaveLength(1);
  });

  it('retries failed image and attachment reads independently in the current document', async () => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:recovered-asset');
    const attempts = new Map<string, number>();
    const user = userEvent.setup();
    const transport = createTransport({
      detail: { ...detail, assets: [imageAsset, attachmentAsset] },
      knowledgeAsset: (input) => {
        const count = (attempts.get(input.assetId) ?? 0) + 1;
        attempts.set(input.assetId, count);
        if (count === 1) throw new Error('asset unavailable');
        const mimeType = input.assetId === imageAsset.id ? 'image/png' : 'text/plain';
        return { ...input, mimeType, byteSize: 3, sha256: '', blob: new Blob(['asset'], { type: mimeType }) };
      },
    });
    renderKnowledge(transport, '/knowledge?base=kb-reading&document=source-1&tab=viewer');
    await user.click(await screen.findByRole('tab', { name: '解析产物' }));
    const imageRetry = await screen.findByRole('button', { name: '重新读取 figure.png' });
    expect(screen.getByRole('button', { name: '重新读取 notes.txt' })).toBeEnabled();
    const reader = document.querySelector('.knowledge-viewer');
    await user.click(imageRetry);
    expect(await screen.findByRole('img', { name: 'figure.png' })).toHaveAttribute('src', 'blob:recovered-asset');
    expect(attempts.get(attachmentAsset.id)).toBe(1);
    await user.click(screen.getByRole('button', { name: '重新读取 notes.txt' }));
    expect(await screen.findByRole('link', { name: '查看 notes.txt' })).toHaveAttribute('href', 'blob:recovered-asset');
    expect(attempts.get(imageAsset.id)).toBe(2);
    expect(attempts.get(attachmentAsset.id)).toBe(2);
    expect(document.querySelector('.knowledge-viewer')).toBe(reader);
    expect(screen.getByRole('tab', { name: '解析产物' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('combobox', { name: '材料' })).toHaveTextContent('source.md');
  });

  it.each(['source', 'asset'] as const)('ignores a late %s retry after switching to another document', async (kind) => {
    const user = userEvent.setup();
    const otherDocument = { ...binaryDocument, id: 'source-2', name: 'other.md', sourceReadPath: '/api/knowledge-bases/kb-reading/documents/source-2/source' };
    const mimeType = kind === 'source' ? 'application/pdf' : 'image/png';
    const payload = (fileId: string) => ({ kbId: base.id, fileId, assetId: imageAsset.id, mimeType, byteSize: 3, sha256: '', blob: new Blob([fileId], { type: mimeType }) });
    const oldPayload = payload('source-1');
    const currentPayload = payload('source-2');
    const createUrl = vi.spyOn(URL, 'createObjectURL').mockImplementation((blob) => blob === oldPayload.blob ? 'blob:old-document' : 'blob:current-document');
    let resolveOld: ((value: typeof oldPayload) => void) | undefined;
    let oldAttempts = 0;
    const read = ({ fileId }: { fileId: string }) => {
      if (fileId === otherDocument.id) return currentPayload;
      if (++oldAttempts === 1) throw new Error('old file unavailable');
      return new Promise<typeof oldPayload>((resolve) => { resolveOld = resolve; });
    };
    const transport = createTransport({
      documents: { items: [binaryDocument, otherDocument] },
      detail: (request: ControlRequest) => ({ ...detail, document: request.params?.fileId === otherDocument.id ? otherDocument : binaryDocument, assets: [imageAsset] }),
      knowledgeDocumentSource: read,
      knowledgeAsset: read,
    });
    renderKnowledge(transport, '/knowledge?base=kb-reading&document=source-1&tab=viewer');
    const tab = kind === 'source' ? '源文件' : '解析产物';
    await user.click(await screen.findByRole('tab', { name: tab }));
    await user.click(await screen.findByRole('button', { name: kind === 'source' ? '重新读取源文件' : '重新读取 figure.png' }));
    await waitFor(() => expect(oldAttempts).toBe(2));
    await user.click(screen.getByRole('combobox', { name: '材料' }));
    await user.click(await screen.findByRole('option', { name: 'other.md' }));
    await user.click(await screen.findByRole('tab', { name: tab }));
    const current = kind === 'source' ? await screen.findByTitle('other.md 源文件') : await screen.findByRole('img', { name: 'figure.png' });
    expect(current).toHaveAttribute('src', 'blob:current-document');
    const reads = kind === 'source' ? transport.knowledgeDocumentSourceCalls : transport.knowledgeAssetCalls;
    expect(reads[1]?.signal?.aborted).toBe(true);

    await act(async () => { resolveOld!(oldPayload); });
    expect(current).toHaveAttribute('src', 'blob:current-document');
    expect(current).toBeInTheDocument();
    expect(createUrl.mock.calls.some(([blob]) => blob === oldPayload.blob)).toBe(false);
    expect(screen.getByRole('combobox', { name: '材料' })).toHaveTextContent('other.md');
  });

  it('cancels a slow search and exposes an actionable recovery state', async () => {
    const user = userEvent.setup();
    const transport = createTransport({
      search: (request: ControlRequest) => new Promise((resolve) => {
        request.signal?.addEventListener('abort', () => resolve({ hits: [] }), { once: true });
      }),
    });
    renderKnowledge(transport, '/knowledge?base=kb-reading&tab=search');
    await user.type(await screen.findByRole('textbox', { name: '搜索知识库' }), '来源');
    await user.click(screen.getByRole('button', { name: '搜索' }));
    expect(await screen.findByRole('button', { name: '取消' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '取消' }));
    expect(await screen.findByText('已取消检索，可以重新搜索。')).toBeInTheDocument();
    const searchRequest = transport.requests.find(({ request }) => request.pathId === 'knowledgeBases.search');
    expect(searchRequest?.request.signal?.aborted).toBe(true);
    expect(screen.getByRole('button', { name: '搜索' })).toBeEnabled();
  });

  it('supports keyboard result selection and a reversible compact result detail', async () => {
    const user = userEvent.setup();
    renderKnowledge(createTransport(), '/knowledge?base=kb-reading&tab=search');
    await user.type(await screen.findByRole('textbox', { name: '搜索知识库' }), '来源');
    await user.click(screen.getByRole('button', { name: '搜索' }));
    const list = await screen.findByRole('listbox', { name: '检索结果' });
    const options = within(list).getAllByRole('option');
    options[0]!.focus();
    await user.keyboard('{ArrowDown}');
    expect(options[1]).toHaveFocus();
    expect(options[1]).toHaveAttribute('aria-selected', 'true');
    await user.keyboard('{Enter}');
    const workspace = document.querySelector('.knowledge-search__results');
    expect(workspace).toHaveAttribute('data-detail-open', 'true');
    // Geometry is checked in the real App window; exercise its compact
    // disclosure state here while the back control is CSS-hidden at wide size.
    await user.click(document.querySelector('.knowledge-search__back') as HTMLButtonElement);
    expect(workspace).not.toHaveAttribute('data-detail-open');
    expect(options[1]).toHaveFocus();
    expect(within(list).getAllByRole('option')).toHaveLength(2);
  });

  it('restores a usable search after leaving a pending query and returning', async () => {
    const user = userEvent.setup();
    const transport = createTransport({ search: () => new Promise(() => {}) });
    renderKnowledge(transport, '/knowledge?base=kb-reading&tab=search');
    await user.type(await screen.findByRole('textbox', { name: '搜索知识库' }), '来源');
    await user.click(screen.getByRole('button', { name: '搜索' }));
    await user.click(screen.getByRole('tab', { name: '资料' }));
    expect(transport.requests.find(({ request }) => request.pathId === 'knowledgeBases.search')?.request.signal?.aborted).toBe(true);
    await user.click(screen.getByRole('tab', { name: '搜索' }));
    expect(await screen.findByRole('textbox', { name: '搜索知识库' })).toHaveValue('来源');
    expect(screen.getByRole('button', { name: '搜索' })).toBeEnabled();
    expect(screen.queryByText('正在查找相关来源…')).not.toBeInTheDocument();
  });

  it('keeps the new search cancellable when an older cancelled request fails late', async () => {
    let rejectOld: (error: Error) => void = () => {};
    let attempts = 0;
    const user = userEvent.setup();
    const transport = createTransport({ search: () => new Promise((_resolve, reject) => {
      if (++attempts === 1) rejectOld = reject;
    }) });
    renderKnowledge(transport, '/knowledge?base=kb-reading&tab=search');
    await user.type(await screen.findByRole('textbox', { name: '搜索知识库' }), '来源');
    await user.click(screen.getByRole('button', { name: '搜索' }));
    await user.click(screen.getByRole('button', { name: '取消' }));
    await user.click(screen.getByRole('button', { name: '搜索' }));
    await act(async () => { rejectOld(new Error('late connection failure')); });
    await user.click(screen.getByRole('button', { name: '取消' }));
    expect(screen.getByRole('button', { name: '搜索' })).toBeEnabled();
    expect(transport.requests.filter(({ request }) => request.pathId === 'knowledgeBases.search').at(-1)?.request.signal?.aborted).toBe(true);
  });

  it('releases an unresponsive search on timeout and permits a new request', async () => {
    const transport = createTransport({ search: () => new Promise(() => {}) });
    renderKnowledge(transport, '/knowledge?base=kb-reading&tab=search');
    const input = await screen.findByRole('textbox', { name: '搜索知识库' });
    vi.useFakeTimers();
    fireEvent.change(input, { target: { value: '来源' } });
    fireEvent.submit(input.closest('form')!);
    await act(async () => { await vi.advanceTimersByTimeAsync(20_100); });
    expect(screen.getByText(/检索超过 20 秒/)).toBeInTheDocument();
    expect(transport.requests.find(({ request }) => request.pathId === 'knowledgeBases.search')?.request.signal?.aborted).toBe(true);
    expect(screen.getByRole('button', { name: '搜索' })).toBeEnabled();
    fireEvent.submit(input.closest('form')!);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(transport.requests.filter(({ request }) => request.pathId === 'knowledgeBases.search')).toHaveLength(2);
  });
});

const base = { id: 'kb-reading', name: '阅读资料', documentCount: 1, chunkCount: 2, status: 'ready' };
const sourceDocument = { id: 'source-1', baseId: base.id, name: 'source.md', status: 'ready', chunkCount: 2, byteSize: 100, mimeType: 'text/markdown' };
const binaryDocument = { ...sourceDocument, sourceReadPath: '/api/knowledge-bases/kb-reading/documents/source-1/source' };
const imageAsset = { id: 'image-1', name: 'figure.png', mimeType: 'image/png', byteSize: 3, readPath: '/v1/knowledge/assets/image-1' };
const attachmentAsset = { id: 'attachment-1', name: 'notes.txt', mimeType: 'text/plain', byteSize: 3, readPath: '/v1/knowledge/assets/attachment-1' };
const detail = { document: sourceDocument, chunks: { items: [{ id: 'chunk-1', content: '第一段来源。' }], total: 2 }, contentWindow: { items: [{ lineNumber: 1, content: '已经读取的来源正文。' }], total: 1 } };

function createTransport(options: { documents?: MockRouteHandler; detail?: MockRouteHandler; search?: MockRouteHandler } & Pick<MockControlTransportOptions, 'knowledgeAsset' | 'knowledgeDocumentSource'> = {}) {
  return new MockControlTransport({ knowledgeAsset: options.knowledgeAsset, knowledgeDocumentSource: options.knowledgeDocumentSource, routes: {
    'knowledgeBases.list': { items: [base] },
    'knowledgeBases.get': { base },
    'knowledgeBases.documents.list': options.documents ?? { items: [sourceDocument] },
    'knowledgeBases.document.get': options.detail ?? detail,
    'knowledgeBases.jobs.list': { items: [] },
    'knowledgeWorker.health': { ok: true, status: 'ready' },
    'knowledgeParsers.list': { items: [] },
    'knowledgeEmbedding.profile': {},
    'configuration.settings': {},
    'knowledgeBases.open': { ok: true },
    'knowledgeBases.search': options.search ?? { hits: [1, 2].map((value) => ({ id: `chunk-${value}`, documentId: sourceDocument.id, documentName: sourceDocument.name, title: `来源段落 ${value}`, excerpt: `可核对摘录 ${value}`, score: .9, page: value })) },
  } });
}

function renderKnowledge(transport: MockControlTransport, route: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<MemoryRouter initialEntries={[route]}><TooltipProvider><ControlTransportProvider transport={transport}><QueryClientProvider client={client}><KnowledgeFeature /></QueryClientProvider></ControlTransportProvider></TooltipProvider></MemoryRouter>);
}
