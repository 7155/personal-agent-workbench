import type { ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import type { ControlRequest, KnowledgeDocumentImportReceipt } from '@/platform/transport';
import { MockControlTransport, type MockRouteHandler } from '@/test/mock-transport';
import { KnowledgeFeature } from './index';
import { knowledgeLibraryKeys } from './api';

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => <div>{data.map((item, index) => <div key={index}>{itemContent(index, item)}</div>)}</div>,
}));

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('Knowledge operation ownership across library navigation', () => {
  it('keeps a late native import receipt in its original library and restores it on return', async () => {
    const pending = deferred<KnowledgeDocumentImportReceipt[]>();
    const transport = createTransport();
    const importFiles = vi.spyOn(transport, 'importKnowledgeDocuments').mockReturnValue(pending.promise);
    const user = userEvent.setup();
    const client = renderKnowledge(transport);
    await user.click(await screen.findByRole('button', { name: '导入文件' }));
    await waitFor(() => expect(importFiles).toHaveBeenCalledOnce());
    await selectBase(user, 'B 资料库');
    await user.click(screen.getByRole('tab', { name: '资料' }));
    expect(screen.getByRole('button', { name: '导入文件' })).toBeEnabled();
    await act(async () => { pending.resolve([receipt('A-import.pdf')]); });
    expect(screen.queryByRole('region', { name: '上传队列' })).not.toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '资料' })).toHaveAttribute('aria-selected', 'true');
    expect(client.getQueryState(knowledgeLibraryKeys.documents(bases[0]!.id))?.isInvalidated).toBe(true);
    expect(client.getQueryState(knowledgeLibraryKeys.documents(bases[1]!.id))?.isInvalidated).toBe(false);
    await selectBase(user, 'A 资料库');
    await user.click(screen.getByRole('tab', { name: '资料' }));
    expect(await screen.findByRole('region', { name: '上传队列' })).toHaveTextContent('A-import.pdf');
    expect(screen.getByText('已进入解析')).toBeInTheDocument();
    expect(importFiles).toHaveBeenCalledOnce();
    expect(importFiles).toHaveBeenCalledWith(expect.objectContaining({ kbId: 'kb-operation-a' }));
  });

  it('does not attach a failed native import to another library or lose its retry feedback', async () => {
    const pending = deferred<KnowledgeDocumentImportReceipt[]>();
    const transport = createTransport();
    vi.spyOn(transport, 'importKnowledgeDocuments').mockReturnValue(pending.promise);
    const user = userEvent.setup();
    renderKnowledge(transport);
    await user.click(await screen.findByRole('button', { name: '导入文件' }));
    await selectBase(user, 'B 资料库');
    await user.click(screen.getByRole('tab', { name: '资料' }));
    await act(async () => { pending.reject(new Error('A 导入通道暂时不可用')); });
    expect(screen.queryByText('A 导入通道暂时不可用')).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: '上传队列' })).not.toBeInTheDocument();
    await selectBase(user, 'A 资料库');
    await user.click(screen.getByRole('tab', { name: '资料' }));
    expect(await screen.findByRole('region', { name: '上传队列' })).toHaveTextContent('A 导入通道暂时不可用');
    expect(screen.getByRole('button', { name: '导入文件' })).toBeEnabled();
  });

  it('keeps simultaneous native imports and their completion order separate', async () => {
    const first = deferred<KnowledgeDocumentImportReceipt[]>();
    const second = deferred<KnowledgeDocumentImportReceipt[]>();
    const transport = createTransport();
    const importFiles = vi.spyOn(transport, 'importKnowledgeDocuments').mockImplementation((input) => input.kbId === bases[0]!.id ? first.promise : second.promise);
    const user = userEvent.setup();
    renderKnowledge(transport);
    await user.click(await screen.findByRole('button', { name: '导入文件' }));
    await selectBase(user, 'B 资料库');
    await user.click(screen.getByRole('tab', { name: '资料' }));
    await user.click(screen.getByRole('button', { name: '导入文件' }));
    await act(async () => { second.resolve([receipt('B-import.pdf', 1)]); });
    expect(await screen.findByRole('region', { name: '上传队列' })).toHaveTextContent('B-import.pdf');
    await act(async () => { first.resolve([receipt('A-import.pdf')]); });
    expect(screen.getByRole('region', { name: '上传队列' })).toHaveTextContent('B-import.pdf');
    expect(screen.getByRole('region', { name: '上传队列' })).not.toHaveTextContent('A-import.pdf');
    await selectBase(user, 'A 资料库');
    await user.click(screen.getByRole('tab', { name: '资料' }));
    expect(await screen.findByRole('region', { name: '上传队列' })).toHaveTextContent('A-import.pdf');
    expect(importFiles).toHaveBeenCalledTimes(2);
  });

  it('does not navigate another library when a rebuild finishes and keeps the receipt on return', async () => {
    const pending = deferred<{ ok: boolean }>();
    const transport = createTransport({ rebuild: () => pending.promise });
    const user = userEvent.setup();
    renderKnowledge(transport);
    await screen.findByRole('button', { name: '导入文件' });
    await openSettings(user);
    await user.click(screen.getByRole('button', { name: '重建索引' }));
    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'knowledgeBases.rebuild')).toBe(true));
    await selectBase(user, 'B 资料库');
    const search = screen.getByRole('textbox', { name: '搜索知识库' });
    await user.type(search, '保留 B 的输入');
    await act(async () => { pending.resolve({ ok: true }); });
    expect(search).toHaveValue('保留 B 的输入');
    expect(screen.getByRole('tab', { name: '搜索' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByText(/已提交.*A 资料库.*索引重建/)).not.toBeInTheDocument();
    await selectBase(user, 'A 资料库');
    expect(await screen.findByText(/已提交.*A 资料库.*索引重建/)).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '搜索' })).toHaveAttribute('aria-selected', 'true');
    await user.click(screen.getByRole('button', { name: '查看处理记录' }));
    expect(screen.getByRole('button', { name: '更多知识库工具' })).toHaveAttribute('data-current-tool', 'jobs');
    expect(transport.requests.filter(({ request }) => request.pathId === 'knowledgeBases.rebuild')).toHaveLength(1);
  });

  it('respects a newer view choice even after returning to the original library', async () => {
    const pending = deferred<{ ok: boolean }>();
    const transport = createTransport({ rebuild: () => pending.promise });
    const user = userEvent.setup();
    renderKnowledge(transport);
    await screen.findByRole('button', { name: '导入文件' });
    await openSettings(user);
    await user.click(screen.getByRole('button', { name: '重建索引' }));
    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'knowledgeBases.rebuild')).toBe(true));
    await selectBase(user, 'B 资料库');
    await selectBase(user, 'A 资料库');
    await openSettings(user);
    expect(screen.getByRole('button', { name: '重建索引' })).toBeDisabled();
    await act(async () => { pending.resolve({ ok: true }); });
    await waitFor(() => expect(screen.getByRole('button', { name: '重建索引' })).toBeEnabled());
    expect(screen.getByRole('button', { name: '更多知识库工具' })).toHaveAttribute('data-current-tool', 'settings');
  });

  it('still opens processing records when the current rebuild owner has not been left', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    renderKnowledge(transport);
    await screen.findByRole('button', { name: '导入文件' });
    await openSettings(user);
    await user.click(screen.getByRole('button', { name: '重建索引' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '更多知识库工具' })).toHaveAttribute('data-current-tool', 'jobs'));
    expect(await screen.findByText(/实际进展见处理记录/)).toBeInTheDocument();
    expect(transport.requests.find(({ request }) => request.pathId === 'knowledgeBases.rebuild')?.request.params).toEqual({ kbId: bases[0]!.id });
  });

});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const bases = [
  { id: 'kb-operation-a', name: 'A 资料库', documentCount: 1, chunkCount: 2, status: 'ready', revision: 1 },
  { id: 'kb-operation-b', name: 'B 资料库', documentCount: 1, chunkCount: 2, status: 'ready', revision: 1 },
];
const documents = bases.map((base, index) => ({
  id: `operation-source-${index}`, baseId: base.id, name: `${index ? 'b' : 'a'}-source.md`,
  status: 'failed', stage: 'parse', error: '需要重新解析', chunkCount: 0, byteSize: 100, mimeType: 'text/markdown', revision: 1,
}));
function receipt(fileName: string, index = 0): KnowledgeDocumentImportReceipt {
  return { kbId: bases[index]!.id, documentId: `import-${index}`, fileName, mimeType: 'application/pdf', byteSize: 100, sha256: 'a'.repeat(64), status: 'queued' };
}
function createTransport(options: { rebuild?: MockRouteHandler } = {}) {
  const selected = (request: ControlRequest) => request.params?.kbId === bases[1]!.id ? 1 : 0;
  return new MockControlTransport({ routes: {
    'knowledgeBases.list': { items: bases },
    'knowledgeBases.get': (request: ControlRequest) => ({ base: bases[selected(request)] }),
    'knowledgeBases.documents.list': (request: ControlRequest) => ({ items: [documents[selected(request)]] }),
    'knowledgeBases.document.get': (request: ControlRequest) => ({ document: documents[selected(request)], chunks: { items: [], total: 0 }, contentWindow: { items: [], total: 0 } }),
    'knowledgeBases.jobs.list': { items: [] },
    'knowledgeWorker.health': { ok: true, status: 'ready' },
    'knowledgeParsers.list': { items: [] },
    'knowledgeEmbedding.profile': {},
    'configuration.settings': {},
    'knowledgeBases.document.retry': { ok: true },
    'knowledgeBases.reindexPreview': { previewToken: 'preview-a', payloadSha256: 'sha256:a', expectedRevision: 1 },
    'knowledgeBases.rebuild': options.rebuild ?? { ok: true },
  } });
}
async function selectBase(user: ReturnType<typeof userEvent.setup>, name: string) {
  const rail = screen.getByRole('complementary', { name: '文档知识库' });
  await user.click(within(rail).getByRole('button', { name: new RegExp(name) }));
  await screen.findByRole('heading', { name });
}
async function openSettings(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: '更多知识库工具' }));
  await user.click(await screen.findByRole('menuitem', { name: '设置' }));
}
function renderKnowledge(transport: MockControlTransport) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<MemoryRouter initialEntries={['/knowledge?base=kb-operation-a&tab=materials']}><TooltipProvider><ControlTransportProvider transport={transport}><QueryClientProvider client={client}><KnowledgeFeature /></QueryClientProvider></ControlTransportProvider></TooltipProvider></MemoryRouter>);
  return client;
}
