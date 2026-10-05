import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import memoryStylesheet from './memory.css?raw';
import { publicMemorySourceLabel } from './public-copy';
import { MemoryFeature } from './index';

afterEach(() => {
  cleanup();
  window.history.replaceState(null, '', '/');
});

describe('MemoryFeature composition', () => {
  it('retains an unsaved profile draft when leaving and returning to its tab', async () => {
    const transport = new MockControlTransport({ routes: {
      'memory.summary': { ok: true }, 'memory.pages': { items: [], nextCursor: '' },
      'memory.profile': { schemaVersion: 'paw.personal-profile.v1', revision: 'profile-1', text: '原背景', truncated: false,
        paragraphs: [{ id: 'card', memoryIds: ['card'], text: '原背景', revision: 'card-1', sourceCount: 0, sourceRefs: [] }] },
      'memory.profile.save': () => { throw new Error('offline'); },
    } });
    renderMemory(transport, '/memory?view=profile');
    const input = await screen.findByRole('textbox', { name: '个人背景 1' });
    fireEvent.change(input, { target: { value: '未保存的个人草稿' } });
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    await screen.findByText(/草稿已保留，重试会核对同一次保存/);
    await userEvent.setup().click(screen.getByRole('tab', { name: '记忆' }));
    expect(screen.queryByRole('textbox', { name: '个人背景 1' })).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole('tab', { name: '关于我' }));
    expect(screen.getByRole('textbox', { name: '个人背景 1' })).toHaveValue('未保存的个人草稿');
    expect(transport.requests.filter(({ request }) => request.pathId === 'memory.profile')).toHaveLength(1);
    expect(screen.getByRole('button', { name: '保存修改' })).toBeEnabled();
  });
  it('uses a restrained full outline for book rows instead of a decorative side stripe', () => {
    expect(memoryStylesheet).not.toMatch(/border-left:\s*3px/);
    expect(memoryStylesheet).toMatch(
      /\[data-layer='books'\] \.memory-layer-list \.mgmt-list__row\s*\{[^}]*border-color:/s,
    );
  });

  it('keeps content classification inside the catalog and lets other views own their workspace', async () => {
    renderMemory(catalogTransport());
    const pipeline = await screen.findByRole('list', { name: '记忆内容分类' });
    expect(within(pipeline).getByRole('button', { name: /来源/ })).toBeInTheDocument();
    expect(within(pipeline).getByRole('button', { name: /主题/ })).toBeInTheDocument();
    cleanup();

    renderMemory(relationsTransport(), '/memory?view=relations');
    expect(await screen.findByRole('heading', { name: '记忆关系' })).toBeInTheDocument();
    expect(screen.queryByRole('list', { name: '记忆内容分类' })).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: '记忆页面' })).not.toBeInTheDocument();
  });

  it('keeps organization and provenance detail behind a disclosure', async () => {
    const user = userEvent.setup();
    renderMemory(catalogTransport());

    await screen.findByRole('list', { name: '记忆内容分类' });
    const toggle = screen.getByRole('button', { name: '整理状态摘要' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('检索索引')).not.toBeInTheDocument();

    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('检索索引')).toBeInTheDocument();
    expect(screen.getByText('主题和记忆都保留来源；打开详情可核对原文和最近的使用记录。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '查看整理与审核' })).toBeVisible();

    await user.click(toggle);
    expect(screen.queryByText('检索索引')).not.toBeInTheDocument();
  });

  it('uses a single selection color and removes the retired pipeline and duplicate navigation styles', () => {
    expect(memoryStylesheet).not.toContain('memory-pipeline');
    expect(memoryStylesheet).not.toContain('memory-app-nav');
    expect(memoryStylesheet).not.toContain('--memory-atoms:');
    expect(memoryStylesheet).toContain('.memory-library__layers button');
  });

  it('routes between memory layers from the catalog classification', async () => {
    const user = userEvent.setup();
    const transport = catalogTransport();
    renderMemory(transport);

    const pipeline = await screen.findByRole('list', { name: '记忆内容分类' });
    await user.click(within(pipeline).getByRole('button', { name: /来源/ }));

    expect(await screen.findByRole('heading', { name: '记忆来源 目录' })).toBeInTheDocument();
    await waitFor(() => expect(transport.requests.some((call) => (
      call.request.pathId === 'memory.pages' && call.request.params?.kind === 'evidence'
    ))).toBe(true));
  });

  it('describes the relations view with the recorded tag count instead of an atom claim', async () => {
    renderMemory(relationsTransport({ memoryTagCount: 6, memoryAtomCount: 40 }), '/memory?view=relations');

    expect(await screen.findByText('6 个关系标签')).toBeInTheDocument();
    expect(screen.queryByText(/参与关联/)).not.toBeInTheDocument();
  });

  it('keeps the preferences status a scope statement instead of a persistence claim', async () => {
    renderMemory(new MockControlTransport({
      capabilities: { features: {} },
      routes: {
        'memory.summary': { ok: true },
        'configuration.settings': {
          ok: true,
          settings: {
            memory: {
              timeDecay: { temporaryHalfLifeDays: 14, stablePreferenceHalfLifeDays: 365 },
              recall: { detailLevel: 'compact' },
              automaticOrganization: { includeAgentDialogue: true },
            },
          },
          runtimeRevision: 3,
        },
      },
    }), '/memory?view=preferences');

    expect(await screen.findByRole('heading', { name: '记忆偏好' })).toBeInTheDocument();
    expect(screen.getByText('影响整理与联想')).toBeInTheDocument();
    expect(screen.queryByText('本机设置持久化')).not.toBeInTheDocument();
  });

  it('keeps relations usable and honest when the shared memory summary fails', async () => {
    renderMemory(relationsTransport(undefined, () => {
      throw new Error('memory summary unavailable');
    }), '/memory?view=relations');

    expect(await screen.findByRole('heading', { name: '记忆关系' })).toBeInTheDocument();
    expect(await screen.findByText('记忆状态暂不可用')).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: /输入词汇，5 条记忆/ })).toBeInTheDocument();
  });

  it('labels an input-method source with the shared catalog vocabulary in relations', async () => {
    renderMemory(relationsTransport(), '/memory?view=relations');

    const node = await screen.findByRole('button', { name: /输入词汇，5 条记忆/ });
    expect(node).toHaveAccessibleName(expect.stringContaining('来源 输入记录'));
    expect(within(node).queryByText('其他来源')).not.toBeInTheDocument();
    expect(publicMemorySourceLabel('rime')).toBe('输入记录');
  });

  it('keeps the Memory identity colour tokens defined in the stylesheet owner', () => {
    for (const token of ['--memory-signal:', '--memory-signal-strong:', '--memory-signal-soft:', '--memory-gold:']) {
      expect(memoryStylesheet).toContain(token);
    }
    expect(memoryStylesheet).toContain('outline-color: var(--memory-signal)');
  });

  it('uses the shared readable text token for catalog section descriptions', () => {
    expect(memoryStylesheet).toContain(
      ".memory-second-brain[data-view='catalog'] .mgmt-section__header p { margin-top: 2px; color: var(--color-text-secondary);",
    );
    expect(memoryStylesheet).not.toContain('color: #7a8493');
    expect(memoryStylesheet).toContain('background: var(--color-surface-subtle);');
    expect(memoryStylesheet).toContain('background: var(--color-paper);');
    expect(memoryStylesheet).toContain(
      '.memory-layer-list .mgmt-list__copy span {\n  display: block;\n  overflow: visible;',
    );
  });

  it('runs the catalog search live instead of behind a filter button', async () => {
    const user = userEvent.setup();
    const transport = catalogTransport();
    renderMemory(transport);

    const search = await screen.findByRole('textbox', { name: '搜索' });
    expect(screen.queryByRole('button', { name: '筛选' })).not.toBeInTheDocument();
    await user.type(search, '偏好');
    await waitFor(() => expect(transport.requests.some((call) => (
      call.request.pathId === 'memory.pages' && call.request.query?.query === '偏好'
    ))).toBe(true));
  });
});

function catalogTransport(): MockControlTransport {
  return new MockControlTransport({
    routes: {
      'memory.summary': { ok: true, memoryAtomCount: 2, memoryBookCount: 1 },
      'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
    },
  });
}

function relationsTransport(
  summaryExtras: Record<string, unknown> = {},
  summaryHandler?: () => unknown,
): MockControlTransport {
  return new MockControlTransport({
    routes: {
      'memory.summary': summaryHandler ?? { ok: true, memoryAtomCount: 2, memoryBookCount: 1, ...summaryExtras },
      'memory.graph.get': (request: ControlRequest) => memoryGraph(String(request.query?.plane ?? '')),
      'memory.entity.get': (request: ControlRequest) => memoryEntity(
        String(request.params?.kind ?? 'tag'),
        String(request.params?.entityId ?? ''),
      ),
    },
  });
}

function memoryGraph(plane: string): Record<string, unknown> {
  const inputTag = graphNode('tag:input-lexicon', 'tag', '输入词汇', 5, '输入法词汇整理', 'rime');
  const localTag = graphNode('tag:memory', 'tag', 'Memory', 3, '记忆组织', 'sqlite');
  const nodes = plane === 'tags'
    ? [inputTag, localTag]
    : [graphNode('group:input', 'group', '输入工程', 8, '输入法整理', 'sqlite'), inputTag, localTag];
  const edges = plane === 'tags'
    ? [{
      id: 'edge:input-memory',
      kind: 'tagRelation',
      sourceId: 'tag:input-lexicon',
      targetId: 'tag:memory',
      sourceKind: 'tag',
      targetKind: 'tag',
      relation: 'related_to',
      weight: 0.8,
      directionBias: 0,
      evidenceCount: 2,
      source: 'sqlite',
      updatedAtMs: 1,
    }]
    : [];
  return {
    schemaVersion: 'rag-ime.memory-graph.v1',
    ok: true,
    settingsRevision: 'settings:test',
    runtimeRevision: 1,
    graphRevision: `sha256:${'a'.repeat(64)}`,
    plane,
    project: 'personal-agent-workbench',
    filters: { status: 'active', query: '', focusId: '', minWeight: 0 },
    nodes,
    edges,
    truncated: { nodes: false, edges: false },
    limits: { nodeLimit: 80, edgeLimit: 160, depth: 1 },
  };
}

function graphNode(
  id: string,
  kind: 'tag' | 'group',
  label: string,
  memberCount: number,
  description: string,
  source: string,
): Record<string, unknown> {
  return {
    id,
    entityId: id.split(':').at(-1),
    kind,
    label,
    description,
    color: kind === 'group' ? 'blue' : 'teal',
    status: 'active',
    source,
    project: 'personal-agent-workbench',
    qualityScore: 1,
    memberCount,
    edgeCount: 1,
    updatedAtMs: 1,
  };
}

function memoryEntity(kind: string, entityId: string): Record<string, unknown> {
  const node = graphNode(
    `${kind}:${entityId}`,
    kind === 'group' ? 'group' : 'tag',
    entityId === 'input-lexicon' ? '输入词汇' : 'Memory',
    5,
    '实体详情',
    entityId === 'input-lexicon' ? 'rime' : 'sqlite',
  );
  return {
    schemaVersion: 'rag-ime.memory-entity.v1',
    ok: true,
    settingsRevision: 'settings:test',
    runtimeRevision: 1,
    kind,
    entityId,
    entityRevision: `sha256:${'b'.repeat(64)}`,
    project: 'personal-agent-workbench',
    entity: node,
    attributes: { type: 'concept', aliases: [], tags: [] },
    connections: { items: [], nextCursor: '', limit: 40, hasMore: false },
    members: { items: [], nextCursor: '', limit: 40, hasMore: false },
    limits: { connectionsLimit: 40, membersLimit: 40 },
  };
}

function renderMemory(transport: MockControlTransport, initialEntry = '/memory?layer=atoms') {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <TooltipProvider delayDuration={0}>
        <ControlTransportProvider transport={transport}>
          <QueryClientProvider client={client}><MemoryFeature /></QueryClientProvider>
        </ControlTransportProvider>
      </TooltipProvider>
    </MemoryRouter>,
  );
}
