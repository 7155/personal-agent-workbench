import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import type { MemoryEntityV1, TopicEntry, TopicReference } from '@/contracts/generated/memory-entity.v1';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { MemoryFeature } from './index';

afterEach(cleanup);

describe('Memory topic reading', () => {
  it('starts with topics and reads current understanding before optional relation exploration', async () => {
    const user = userEvent.setup();
    const transport = topicTransport();
    renderMemory(transport, '/memory');
    expect(await screen.findByRole('heading', { name: '长期主题 目录' })).toBeInTheDocument();
    expect(transport.requests.find((item) => item.request.pathId === 'memory.pages')?.request.params?.kind).toBe('books');
    await user.click(await screen.findByRole('button', { name: /输入辅助/ }));
    const topic = await screen.findByRole('region', { name: '输入辅助 主题页' });
    expect(await within(topic).findByRole('heading', { name: '当前认识' })).toBeInTheDocument();
    expect(within(topic).getByText('目前采用模型 B，仅用于本周实验。')).toBeInTheDocument();
    expect(within(topic).getByText('不得替换现有输入内核。')).toBeInTheDocument();
    expect(within(topic).getByText('延迟过高时是否改用模型 C？')).toBeInTheDocument();
    expect(within(topic).queryByText('旧摘要仍说采用模型 A。')).not.toBeInTheDocument();
    const headings = within(topic).getAllByRole('heading').map((item) => item.textContent);
    expect(headings.indexOf('当前认识')).toBeLessThan(headings.indexOf('最近变化'));
    expect(headings.indexOf('最近变化')).toBeLessThan(headings.indexOf('仍未确定'));
    expect(transport.requests.some((item) => item.request.pathId === 'memory.graph.get')).toBe(false);
    await user.click(within(topic).getByText('探索主题关系', { selector: 'summary' }));
    expect(await within(topic).findByText('标签共现用于查找相关主题，不表示事实支持或因果关系。')).toBeInTheDocument();
    await waitFor(() => expect(transport.requests.some((item) => item.request.pathId === 'memory.graph.get' && item.request.query?.query === '输入辅助')).toBe(true));
    expect(await within(topic).findByText('当前导航筛选没有可读关系；主题依据仍可在上方核对。')).toBeInTheDocument();
    await user.click(within(topic).getByRole('button', { name: '查看全部导航关系' }));
    await waitFor(() => expect(transport.requests.some((item) => item.request.pathId === 'memory.graph.get' && !item.request.query?.query)).toBe(true));
    expect(within(topic).getByRole('textbox', { name: '筛选分组或标签' })).toHaveValue('');
    expect(transport.requests.every((item) => !item.request.pathId.includes('run') && !item.request.pathId.includes('build'))).toBe(true);
  });

  it('keeps catalog controls in the list pane so the selected topic has its own reading column', async () => {
    const user = userEvent.setup();
    renderMemory(topicTransport());
    const list = await screen.findByRole('complementary', { name: '长期主题目录' });
    expect(within(list).getByRole('heading', { name: '长期主题 目录' })).toBeInTheDocument();
    expect(within(list).getByRole('textbox', { name: '搜索' })).toBeInTheDocument();
    await user.click(screen.getByText(/^筛选 ·/));
    expect(within(list).getByRole('combobox', { name: '状态' })).toBeInTheDocument();
    expect(within(list).getByRole('combobox', { name: '归属' })).toBeInTheDocument();
    await user.click(await within(list).findByRole('button', { name: /输入辅助/ }));
    const topic = await screen.findByRole('region', { name: '输入辅助 主题页' });
    expect(await within(topic).findByRole('heading', { name: '当前认识' })).toBeInTheDocument();
    expect(list).not.toContainElement(topic);
    expect(list.parentElement).toBe(topic.closest('.memory-layer-detail')?.parentElement);
  });

  it('returns to the compact catalog without losing its book, search, status or owner filter', async () => {
    const user = userEvent.setup();
    const transport = topicTransport();
    renderMemory(transport);
    const search = await screen.findByRole('textbox', { name: '搜索' });
    await user.type(search, '输入');
    await waitFor(() => expect(transport.requests.filter((item) => item.request.pathId === 'memory.pages').at(-1)?.request.query?.query).toBe('输入'));
    await user.click(screen.getByText(/^筛选 ·/));
    await user.click(await screen.findByRole('combobox', { name: '状态' }));
    await user.click(await screen.findByRole('option', { name: '使用中' }));
    await user.click(await screen.findByRole('combobox', { name: '归属' }));
    await user.click(screen.getByRole('option', { name: /测试伙伴/ }));
    await user.click(await screen.findByRole('button', { name: /输入辅助/ }));
    expect(await screen.findByText('目前采用模型 B，仅用于本周实验。')).toBeInTheDocument();
    const workspace = document.querySelector('.memory-layer-workspace');
    expect(workspace).toHaveAttribute('data-detail-open', 'true');
    // The compact back control is CSS-hidden at jsdom's default wide size;
    // exercise its state transition here, with window geometry covered by UI QA.
    const back = document.querySelector('.memory-layer-detail__back') as HTMLButtonElement;
    expect(back).toHaveAttribute('aria-label', '返回记忆目录');
    await user.click(back);
    expect(workspace).not.toHaveAttribute('data-detail-open');
    expect(screen.getByTestId('memory-location')).toHaveTextContent('?layer=books&id=book-1');
    expect(screen.getByRole('textbox', { name: '搜索' })).toHaveValue('输入');
    expect(screen.getByRole('combobox', { name: '状态' })).toHaveTextContent('使用中');
    expect(screen.getByRole('combobox', { name: '归属' })).toHaveTextContent('测试伙伴');
    const row = screen.getByRole('button', { name: /输入辅助/ });
    expect(row).toHaveAttribute('data-selected', 'true');
    await user.click(row);
    expect(workspace).toHaveAttribute('data-detail-open', 'true');
    await waitFor(() => expect(transport.requests.filter((item) => item.request.pathId === 'memory.pages').at(-1)?.request.query).toMatchObject({ query: '输入', status: 'active', ownerId: 'partner-1' }));
  });

  it('counts each original source once while keeping its Atom as a separate memory-original action', async () => {
    const user = userEvent.setup();
    const entity = topicEntity();
    const self: TopicReference = { kind: 'atom', id: 'atom-current', referenceKind: 'atom', referenceId: 'atom-current' };
    entity.topicPage!.sections.current[0]!.references = [self, source(), source()];
    entity.topicPage!.sources = [self, source(), source()];
    renderMemory(topicTransport(() => entity));
    await user.click(await screen.findByRole('button', { name: /输入辅助/ }));
    const current = await screen.findByRole('region', { name: '当前认识' });
    await user.click(within(current).getByText('查看依据 · 1 条来源', { selector: 'summary' }));
    expect(within(current).getAllByRole('button', { name: '查看记忆原文' })).toHaveLength(1);
    expect(within(current).getAllByRole('button', { name: '本周模型调整原话' })).toHaveLength(1);
    expect(within(current).queryByRole('button', { name: '查看原始来源' })).not.toBeInTheDocument();
    const sources = screen.getByRole('region', { name: '主题来源' });
    await user.click(within(sources).getByText('核对原始来源 · 1 条', { selector: 'summary' }));
    expect(within(sources).getAllByRole('button')).toHaveLength(1);
  });

  it('follows an exact entry source without inventing a reason for the recorded change', async () => {
    const user = userEvent.setup();
    const transport = topicTransport();
    renderMemory(transport);
    await user.click(await screen.findByRole('button', { name: /输入辅助/ }));
    const topic = await screen.findByRole('region', { name: '输入辅助 主题页' });
    const history = await within(topic).findByRole('region', { name: '最近变化' });
    expect(within(history).getByText('此前采用模型 A。')).toBeInTheDocument();
    expect(within(history).getByText('尚未提供可核对的变更理由。')).toBeInTheDocument();
    await user.click(within(topic).getByText('查看依据 · 1 条来源', { selector: 'summary' }));
    await user.click(within(topic).getByRole('button', { name: '本周模型调整原话' }));
    const dialog = await screen.findByRole('dialog', { name: '本周模型调整原话' });
    expect(await within(dialog).findByText('这周先用 B 对照，其他范围不变。')).toBeInTheDocument();
    expect(transport.requests.find((item) => item.request.pathId === 'memory.reference.get')?.request.params).toEqual({ kind: 'evidence', referenceId: 'evidence-adjustment' });
  });

  it('does not turn an older backend summary into a current conclusion', async () => {
    const user = userEvent.setup();
    const legacy = topicEntity();
    delete legacy.topicPage;
    renderMemory(topicTransport(() => legacy));
    await user.click(await screen.findByRole('button', { name: /输入辅助/ }));
    const topic = await screen.findByRole('region', { name: '输入辅助 主题页' });
    expect(await within(topic).findByText('当前服务尚未提供主题的当前认识。')).toBeInTheDocument();
    expect(within(topic).queryByRole('heading', { name: '当前认识' })).not.toBeInTheDocument();
    await user.click(within(topic).getByText('查看已有摘要', { selector: 'summary' }));
    expect(within(topic).getByText('旧摘要仍说采用模型 A。')).toBeInTheDocument();
    expect(within(topic).getByText('这份摘要仅供回看，尚未核对为当前认识。')).toBeInTheDocument();
  });

  it('keeps loading and failures truthful and retries only the topic read', async () => {
    const user = userEvent.setup();
    const read = vi.fn().mockRejectedValueOnce(new Error('/private/runtime/topic failure')).mockResolvedValue(topicEntity());
    const transport = topicTransport(read);
    renderMemory(transport);
    await user.click(await screen.findByRole('button', { name: /输入辅助/ }));
    const topic = await screen.findByRole('region', { name: '输入辅助 主题页' });
    expect(await within(topic).findByRole('alert')).toHaveTextContent('主题内容读取失败');
    expect(within(topic).queryByText('/private/runtime/topic failure')).not.toBeInTheDocument();
    expect(within(topic).queryByRole('heading', { name: '当前认识' })).not.toBeInTheDocument();
    await user.click(within(topic).getByRole('button', { name: '重新读取主题' }));
    expect(await within(topic).findByText('目前采用模型 B，仅用于本周实验。')).toBeInTheDocument();
    expect(read).toHaveBeenCalledTimes(2);
  });

  it('marks partial projection coverage and unavailable sources without making an all-clear claim', async () => {
    const user = userEvent.setup();
    const entity = topicEntity();
    const page = entity.topicPage!;
    page.freshness = 'needs_refresh';
    page.coverage = { memberCount: 9, visibleAtomCount: 1, omittedAtomCount: 8, truncated: true };
    page.sections.current[0]!.references = [];
    page.sections.current[0]!.sourceStatus = 'unavailable';
    page.sections.constraints = [];
    page.sections.openQuestions = [];
    page.sections.history = [];
    page.sources = [];
    renderMemory(topicTransport(() => entity));
    await user.click(await screen.findByRole('button', { name: /输入辅助/ }));
    const topic = await screen.findByRole('region', { name: '输入辅助 主题页' });
    expect(await within(topic).findByText('主题需要更新')).toBeInTheDocument();
    expect(within(topic).getByText('展示 1 条可读记忆；主题原关联 9 条。另有 8 条未展示。')).toBeInTheDocument();
    expect(within(topic).getByText('这条记忆的来源暂不可读。')).toBeInTheDocument();
    expect(within(topic).queryByRole('heading', { name: '仍未确定' })).not.toBeInTheDocument();
    expect(within(topic).queryByText('没有未确定的问题')).not.toBeInTheDocument();
  });

  it('labels a superseded deep link as historical while showing the atom projection', async () => {
    const user = userEvent.setup();
    const entity = topicEntity();
    entity.entity.status = 'superseded';
    renderMemory(topicTransport(() => entity));
    await user.click(await screen.findByRole('button', { name: /输入辅助/ }));
    const topic = await screen.findByRole('region', { name: '输入辅助 主题页' });
    expect(within(topic).getByText('这是已替代的历史主题。')).toBeInTheDocument();
    expect(within(topic).getByText('以下内容按可读记忆原子重建，原主题摘要不作为当前认识。')).toBeInTheDocument();
    expect(within(topic).getByRole('heading', { name: '当前认识' })).toBeInTheDocument();
    expect(within(topic).getByRole('heading', { name: '最近变化' })).toBeInTheDocument();
  });

  it('opens a linked topic directly after reload without requiring its row on the first page', async () => {
    const transport = topicTransport();
    renderMemory(transport, '/memory?layer=books&id=book-direct');
    expect(await screen.findByText('目前采用模型 B，仅用于本周实验。')).toBeInTheDocument();
    expect(transport.requests.find((item) => item.request.pathId === 'memory.entity.get')?.request.params).toEqual({ kind: 'book', entityId: 'book-direct' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('keeps a pending read out of the current-conclusion surface until the real projection arrives', async () => {
    const user = userEvent.setup();
    let finish!: (value: MemoryEntityV1) => void;
    const pending = new Promise<MemoryEntityV1>((resolve) => { finish = resolve; });
    renderMemory(topicTransport(() => pending));
    await user.click(await screen.findByRole('button', { name: /输入辅助/ }));
    const topic = await screen.findByRole('region', { name: '输入辅助 主题页' });
    expect(within(topic).getByRole('status')).toHaveTextContent('正在读取主题认识');
    expect(within(topic).queryByRole('heading', { name: '当前认识' })).not.toBeInTheDocument();
    expect(within(topic).queryByText('旧摘要仍说采用模型 A。')).not.toBeInTheDocument();
    await act(async () => finish(topicEntity()));
    expect(await within(topic).findByRole('heading', { name: '当前认识' })).toBeInTheDocument();
  });

  it('retains the existing sensitive-content boundary without loading a topic body', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: {
      'memory.summary': { ok: true, memoryBookCount: 1 },
      'memory.pages': { ok: true, items: [{ id: 'book-sensitive', title: '受限主题', sensitive: true, summary: '不可显示正文', status: 'active', type: 'topic' }], nextCursor: '', limit: 50 },
    } });
    renderMemory(transport);
    await user.click(await screen.findByRole('button', { name: /受限主题/ }));
    const topic = await screen.findByRole('region', { name: '受限主题 主题页' });
    expect(within(topic).getByRole('status')).toHaveTextContent('隐私策略已隐藏');
    expect(topic).not.toHaveTextContent('不可显示正文');
    expect(within(topic).queryByText('查看已有摘要')).not.toBeInTheDocument();
    expect(transport.requests.some((item) => item.request.pathId === 'memory.entity.get')).toBe(false);
  });
});

function entry(id: string, text: string, kind = 'project_decision'): TopicEntry {
  return { id, text, kind, status: 'current', claimState: 'current', atomIds: [id], references: [], sourceStatus: 'unavailable', lineageId: 'lineage-model', validFromMs: 1788566400000, validToMs: null, supersedesId: '', supersededByIds: [], reason: null };
}

function source(): TopicReference { return { kind: 'evidence', id: 'evidence-adjustment', referenceKind: 'evidence', referenceId: 'evidence-adjustment', label: '本周模型调整原话' }; }

function topicEntity(bookId = 'book-1'): MemoryEntityV1 {
  const current: TopicEntry = { ...entry('atom-current', '目前采用模型 B，仅用于本周实验。'), references: [source()], sourceStatus: 'available', supersedesId: 'atom-old' };
  const history: TopicEntry = { ...entry('atom-old', '此前采用模型 A。'), status: 'superseded', claimState: 'superseded', validToMs: 1788566400000, supersededByIds: ['atom-current'] };
  return {
    schemaVersion: 'rag-ime.memory-entity.v1', settingsRevision: 'settings:test', runtimeRevision: 1,
    ok: true, kind: 'book', entityId: bookId, entityRevision: `sha256:${'a'.repeat(64)}`, project: '',
    entity: { id: `book:${bookId}`, entityId: bookId, kind: 'book', label: '输入辅助', status: 'active', description: '旧摘要仍说采用模型 A。', color: 'blue', source: 'sqlite', project: '', qualityScore: 1, memberCount: 4, edgeCount: 0, updatedAtMs: 1 },
    attributes: { type: 'topic', aliases: [], tags: [] },
    connections: { items: [], nextCursor: '', hasMore: false, limit: 40 },
    members: { items: [], nextCursor: '', hasMore: false, limit: 40 },
    limits: { connectionsLimit: 40, membersLimit: 40 },
    topicPage: {
      schemaVersion: 'rag-ime.memory-topic-page.v1', bookId, revision: `sha256:${'b'.repeat(64)}`, authority: 'atom_projection', freshness: 'current', summary: '目前采用模型 B，仅用于本周实验。',
      sections: { current: [current], constraints: [entry('atom-constraint', '不得替换现有输入内核。', 'project_constraint')], openQuestions: [entry('atom-question', '延迟过高时是否改用模型 C？', 'project_question')], history: [history] },
      sources: [source()], coverage: { memberCount: 4, visibleAtomCount: 4, omittedAtomCount: 0, truncated: false },
    },
  };
}

function topicTransport(read: (request: ControlRequest) => unknown = (request) => topicEntity(String(request.params?.entityId ?? 'book-1'))) {
  return new MockControlTransport({ routes: {
    'memory.summary': { ok: true, memoryBookCount: 1, currentAtomCount: 3, owners: [{ ownerKind: 'partner', ownerId: 'partner-1', ownerDisplayName: '测试伙伴', itemCount: 1 }] },
    'memory.pages': { ok: true, items: [{ id: 'book-1', title: '输入辅助', summary: '旧摘要仍说采用模型 A。', type: 'topic', status: 'active' }], nextCursor: '', limit: 50 },
    'memory.entity.get': read,
    'memory.graph.get': (request: ControlRequest) => ({ schemaVersion: 'rag-ime.memory-graph.v1', ok: true, settingsRevision: 'settings:test', runtimeRevision: 1, graphRevision: `sha256:${'c'.repeat(64)}`, plane: request.query?.plane, project: '', filters: { status: 'active', query: '输入辅助', focusId: '', minWeight: 0 }, nodes: [], edges: [], truncated: { nodes: false, edges: false }, limits: { nodeLimit: 80, edgeLimit: 160, depth: 1 } }),
    'memory.reference.get': { schemaVersion: 'rag-ime.memory-reference.v1', settingsRevision: 'settings:test', runtimeRevision: 1, ok: true, kind: 'evidence', referenceId: 'evidence-adjustment', ref: source(), source: { kind: 'agent_capture', id: 'evidence-adjustment' }, item: { id: 'evidence-adjustment', title: '本周模型调整原话', status: 'active', text: '这周先用 B 对照，其他范围不变。' }, evidenceRefs: [] },
  } });
}

function renderMemory(transport: MockControlTransport, initialEntry = '/memory?layer=books') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<MemoryRouter initialEntries={[initialEntry]}><TooltipProvider delayDuration={0}><ControlTransportProvider transport={transport}><QueryClientProvider client={client}><MemoryFeature /><LocationProbe /></QueryClientProvider></ControlTransportProvider></TooltipProvider></MemoryRouter>);
}

function LocationProbe() { return <output data-testid="memory-location">{useLocation().search}</output>; }
