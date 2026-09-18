import type { ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { PawOsAppSurfaceProvider } from '@/features/paw-os/surface-context';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { KnowledgeFeature } from './index';

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso-list">{data.map((item, index) => <div key={index}>{itemContent(index, item)}</div>)}</div>
  ),
}));

beforeEach(() => {
  vi.spyOn(URL, 'createObjectURL').mockImplementation((blob) => blob instanceof Blob && blob.type === 'application/pdf' ? 'blob:knowledge-source' : 'blob:knowledge-asset');
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  document.querySelectorAll('[data-test-paw-desktop-root]').forEach((element) => element.remove());
  vi.restoreAllMocks();
});

describe('document knowledge library', () => {
  it('collapses the actual App library index while retaining the current search draft and a library selector', async () => {
    const user = userEvent.setup();
    renderKnowledge(createTransport(), '/knowledge', true);
    const query = await screen.findByRole('textbox', { name: '搜索知识库' });
    await user.type(query, '保留检索草稿');
    const rail = screen.getByRole('complementary', { name: '文档知识库' });
    await user.click(screen.getByRole('button', { name: '收起知识库目录' }));
    expect(rail).not.toBeVisible();
    expect(query).toHaveValue('保留检索草稿');
    expect(screen.getByRole('combobox', { name: '当前知识库' })).toBeVisible();
    await user.click(screen.getByRole('button', { name: '展开知识库目录' }));
    expect(rail).toBeVisible();
    expect(query).toHaveValue('保留检索草稿');
  });
  it('starts with search and reveals configuration only from the library tools menu', async () => {
    const user = userEvent.setup();
    const transport = createTransport();
    renderKnowledge(transport, '/knowledge', true);
    expect(await screen.findByRole('textbox', { name: '搜索知识库' })).toBeVisible();
    expect(screen.getByRole('tab', { name: '搜索' })).toHaveAttribute('data-state', 'active');
    expect(screen.queryByRole('tab', { name: '设置' })).not.toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: '设置' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '删除知识库' })).not.toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.pathId === 'knowledgeBases.search')).toBe(false);
    await openKnowledgeTool(user, '设置');
    expect(screen.getByRole('tabpanel', { name: '知识库设置' })).toBeVisible();
    expect(await screen.findByRole('button', { name: '保存基本信息' })).toBeVisible();
  });

  it.each([false, true])('returns to the selected search result without searching again (route remount: %s)', async (remountOnRoute) => {
    const user = userEvent.setup();
    const transport = createTransport({ multipleSearchHits: true });
    renderKnowledge(transport, '/knowledge?tab=search', true, true, remountOnRoute);

    const query = await screen.findByRole('textbox', { name: '搜索知识库' });
    await user.type(query, '工具如何注册');
    await user.click(screen.getByRole('button', { name: '搜索' }));
    const resultList = await screen.findByRole('listbox', { name: '检索结果' });
    await user.click(within(resultList).getAllByRole('option')[1]!);
    await user.type(query, '，只看运行时');
    await user.click(screen.getByRole('button', { name: '打开来源' }));

    expect(await screen.findByText(/已定位检索命中/u)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '返回搜索结果' }));
    expect(await screen.findByRole('textbox', { name: '搜索知识库' })).toHaveValue('工具如何注册，只看运行时');
    const restoredResults = await screen.findByRole('listbox', { name: '检索结果' });
    const selected = within(restoredResults).getByRole('option', { selected: true });
    expect(selected).toHaveTextContent('工具注册');
    expect(within(restoredResults).getAllByRole('option')).toHaveLength(2);
    expect(transport.requests.filter(({ request }) => request.pathId === 'knowledgeBases.search')).toHaveLength(1);
    expect(request(transport, 'knowledgeBases.search')?.body).toMatchObject({ query: '工具如何注册' });
  });

  it('keeps the materials filter when returning from source reading after a native route remount', async () => {
    const user = userEvent.setup();
    renderKnowledge(createTransport(), '/knowledge?tab=materials', true, true, true);
    const filter = await screen.findByRole('textbox', { name: '筛选文件' });
    await user.type(filter, 'RUNTIME');
    await user.click(screen.getByRole('button', { name: '查看 runtime.pdf' }));
    await user.click(await screen.findByRole('button', { name: '返回资料' }));
    expect(await screen.findByRole('textbox', { name: '筛选文件' })).toHaveValue('RUNTIME');
    expect(screen.getByRole('button', { current: true })).toHaveTextContent('runtime.pdf');
  });

  it('opens the exact hit even when the current materials list does not contain its document', async () => {
    const user = userEvent.setup();
    const transport = createTransport({ unlistedSearchSource: true });
    renderKnowledge(transport, '/knowledge?tab=search', true, true, true);
    await user.type(await screen.findByRole('textbox', { name: '搜索知识库' }), '工具注册');
    await user.click(screen.getByRole('button', { name: '搜索' }));
    await user.click(await screen.findByRole('button', { name: '打开来源' }));
    expect(await screen.findByText(/已定位检索命中/u)).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: '材料' })).toHaveTextContent('runtime.pdf');
    await user.click(screen.getByRole('button', { name: '返回搜索结果' }));
    expect(await screen.findByRole('textbox', { name: '搜索知识库' })).toHaveValue('工具注册');
    expect(transport.requests.filter(({ request }) => request.pathId === 'knowledgeBases.search')).toHaveLength(1);
  });

  it('does not start Knowledge queries while its PAWOS window is inactive', async () => {
    const transport = createTransport();
    renderKnowledge(transport, '/knowledge', true, false);
    await new Promise((resolve) => window.setTimeout(resolve, 0));

    expect(transport.requests.some((call) => String(call.request.pathId).startsWith('knowledge'))).toBe(false);
  });

  it('uses one PAWOS navigation layer while keeping the real library tabs interactive', async () => {
    const user = userEvent.setup();
    renderKnowledge(createTransport(), '/knowledge', true);

    const band = await screen.findByRole('region', { name: '切换文档知识库' });

    // A window wide enough for the rail uses the rail as its library selector,
    // so the command band names the current library instead of repeating a
    // select beside it. The labelled 当前知识库 selector stays in the same DOM
    // behind the container query that narrow windows switch on — nothing here
    // re-renders on resize.
    expect(within(band).getByText('伙伴运行资料')).toBeVisible();
    const narrowSelector = screen.getByRole('combobox', { hidden: true, name: '当前知识库' });
    expect(narrowSelector).toHaveTextContent('伙伴运行资料');
    expect(narrowSelector).not.toBeVisible();

    // The rail carries the index only: one window may never offer two
    // identically named 刷新知识库 or 新建知识库 buttons.
    const rail = screen.getByRole('complementary', { name: '文档知识库' });
    expect(within(rail).getByRole('button', { name: /伙伴运行资料/ })).toHaveAttribute('aria-current', 'page');
    expect(within(rail).queryByRole('button', { name: '刷新知识库' })).not.toBeInTheDocument();
    expect(within(rail).queryByRole('button', { name: '新建知识库' })).not.toBeInTheDocument();
    expect(screen.getAllByRole('button', { hidden: true, name: '刷新知识库' })).toHaveLength(1);

    await openKnowledgeTool(user, '知识图谱');
    expect(await screen.findByRole('button', { name: /重建图谱/ })).toBeInTheDocument();
    await openKnowledgeTool(user, '设置');
    expect(await screen.findByRole('button', { name: '保存基本信息' })).toBeInTheDocument();
  });

  it('keeps document retrieval separate from personal memory and exposes citations', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    renderKnowledge(transport);

    expect(await screen.findByRole('complementary', { name: '文档知识库' })).toBeInTheDocument();
    expect(screen.getByRole('main')).toHaveClass('knowledge-feature');
    expect(await screen.findByRole('heading', { name: '伙伴运行资料', level: 2 })).toBeInTheDocument();
    const materialDetails = screen.getByText('高级：材料详情', { selector: 'summary' });
    expect(materialDetails).toBeInTheDocument();
    expect(screen.getByText('PDF 文档')).toBeInTheDocument();
    expect(screen.queryByText('application/pdf')).not.toBeInTheDocument();
    await user.click(materialDetails);
    expect(screen.getByText('application/pdf')).toBeVisible();
    await user.click(materialDetails);
    expect((await screen.findAllByText('runtime.pdf')).length).toBeGreaterThan(0);
    expect(transport.requests.some((call) => call.request.pathId.startsWith('memory.'))).toBe(false);
    expect(transport.requests.some((call) => call.request.pathId === 'knowledge.routeStatus')).toBe(false);

    await user.click(screen.getByRole('tab', { name: '搜索' }));
    await user.type(screen.getByRole('textbox', { name: '搜索知识库' }), '工具如何注册');
    await user.click(screen.getByRole('button', { name: '搜索' }));
    expect(await screen.findByRole('option', { name: /工具注册/ })).toBeInTheDocument();
    expect(screen.getByText('“工具如何注册” · 1 条结果')).toBeInTheDocument();
    expect(screen.getByText('高相关')).toHaveAttribute('data-level', 'high');
    expect(screen.getByRole('region', { name: '本次召回诊断' })).toHaveTextContent('Reranker本次未重排');
    expect(screen.getByRole('region', { name: '本次召回诊断' })).toHaveTextContent('每路候选上限40');
    await user.click(screen.getByText('高级：检索详情', { selector: 'summary' }));
    expect(screen.getByText('92 / 100')).toBeInTheDocument();
    expect(screen.getByText('混合检索 · 关键词候选第 1 · 向量候选第 2 · 图谱候选第 1 · 关联 工具、知识整理服务')).toBeInTheDocument();
    expect(screen.getByText('工具 → mentions → 文档片段；文档片段 → evidence → 工具注册')).toBeInTheDocument();
    expect(screen.queryByText('知识整理服务 → uses → 检索器')).not.toBeInTheDocument();
    await user.click(screen.getByText('显示 2 / 共 4 条 · 查看全部'));
    expect(screen.getByText('知识整理服务 → uses → 检索器')).toBeVisible();
    expect(screen.getByText('检索器 → reads → runtime.pdf')).toBeVisible();
    expect(screen.getByText('第 12 页')).toBeInTheDocument();
    expect(screen.getByText('伙伴工作循环 > 工具')).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent('Knowledge Worker');
    const search = request(transport, 'knowledgeBases.search');
    expect(search?.params).toEqual({ kbId: 'kb-runtime' });
    expect(search?.body).toEqual({ query: '工具如何注册', topK: 10, mode: 'hybrid', threshold: 0.2, rerank: false, rerankCandidateDepth: 40 });

    await user.click(screen.getByRole('button', { name: '打开来源' }));
    await waitFor(() => expect(transport.requests.find((call) => call.request.pathId === 'knowledgeBases.open' && call.request.query?.chunkId === 'chunk-tool')?.request).toMatchObject({
      params: { kbId: 'kb-runtime', fileId: 'file-runtime' },
      query: { chunkId: 'chunk-tool', page: 12, lines: 80 },
    }));
    await waitFor(() => expect(screen.getByRole('tab', { name: '查看材料' })).toHaveAttribute('data-state', 'active'));
    expect(await screen.findByText(/已定位检索命中/)).toBeInTheDocument();
    expect(screen.getByText('伙伴启动时注册 knowledge。').closest('article')).toHaveAttribute('data-focused', 'true');
  });

  it('imports through the typed transport and updates Agent/parser settings with revisions', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    renderKnowledge(transport);

    expect((await screen.findAllByText('runtime.pdf')).length).toBeGreaterThan(0);
    await user.click(screen.getByRole('button', { name: '导入文件' }));
    await waitFor(() => expect(transport.knowledgeImportCalls).toEqual([{
      kbId: 'kb-runtime',
      parserProvider: 'auto',
      maxFiles: 20,
    }]));
    expect(await screen.findByRole('region', { name: '上传队列' })).toHaveTextContent('new.pdf');
    expect(screen.getByText('已进入解析')).toBeInTheDocument();

    await openKnowledgeTool(user, '设置');
    const agentSwitch = screen.getByRole('switch', { name: '允许伙伴检索此知识库' });
    expect(agentSwitch).not.toBeChecked();
    await user.click(agentSwitch);
    await waitFor(() => expect(request(transport, 'knowledgeBases.update')?.body).toMatchObject({
      agentEnabled: true,
      expectedRevision: 8,
    }));

    await user.click(screen.getByRole('combobox', { name: '解析方式' }));
    await user.click(await screen.findByRole('option', { name: 'MinerU' }));
    await waitFor(() => expect(transport.requests.filter((call) => call.request.pathId === 'knowledgeBases.update').at(-1)?.request.body).toMatchObject({
      parserProvider: 'mineru_local_http',
      expectedRevision: 8,
    }));
    expect(screen.getByText('已连接')).toBeInTheDocument();

    const chunkSize = screen.getByRole('spinbutton', { name: '大小' });
    await user.clear(chunkSize);
    await user.type(chunkSize, '1400');
    await user.click(screen.getByRole('button', { name: '保存切分设置' }));
    await waitFor(() => expect(transport.requests.filter((call) => call.request.pathId === 'knowledgeBases.update').at(-1)?.request.body).toMatchObject({
      chunkingConfig: { strategy: 'markdown', size: 1_400, overlap: 160, respectHeadings: true, respectPageBoundaries: true },
      expectedRevision: 8,
    }));

    await user.click(screen.getByText('高级：检索调优', { selector: 'summary' }));
    const denseWeight = screen.getByRole('spinbutton', { name: '向量权重' });
    await user.clear(denseWeight);
    await user.type(denseWeight, '1.5');
    const graphSwitch = screen.getByRole('switch', { name: '启用图谱增强' });
    expect(graphSwitch).toBeChecked();
    await user.click(graphSwitch);
    await user.click(screen.getByRole('button', { name: '保存检索设置' }));
    await waitFor(() => expect(transport.requests.filter((call) => call.request.pathId === 'knowledgeBases.update').at(-1)?.request.body).toMatchObject({
      retrievalConfig: {
        mode: 'hybrid', topK: 10, threshold: .2, lexicalWeight: 1, denseWeight: 1.5,
        graphEnabled: false, graphWeight: .7, rrfK: 60, candidateMultiplier: 4, rerankEnabled: false, rerankCandidateDepth: 40,
      },
      expectedRevision: 8,
    }));
    await user.click(screen.getByText('高级：连接与索引设置', { selector: 'summary' }));
    expect(screen.getByDisplayValue('local-hash:96:v1')).toBeDisabled();
    expect(screen.getByDisplayValue('42')).toBeDisabled();

    await user.click(screen.getByRole('button', { name: '重建索引' }));
    await waitFor(() => expect(request(transport, 'knowledgeBases.reindexPreview')).toBeDefined());
    await waitFor(() => expect(request(transport, 'knowledgeBases.rebuild')?.body).toEqual({
      previewToken: 'preview-reindex', payloadSha256: 'sha256:reindex', expectedRevision: 8, confirmText: 'REBUILD',
    }));
    await waitFor(() => expect(screen.getByRole('button', { name: '更多知识库工具' })).toHaveAttribute('data-current-tool', 'jobs'));
  });

  it('tests, saves, and rolls back a global vector model configuration without sending a secret', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    renderKnowledge(transport, '/knowledge?tab=settings');

    expect(await screen.findByRole('region', { name: '向量模型与索引' })).toBeInTheDocument();
    expect(screen.getByText('连接状态')).toBeInTheDocument();
    expect(screen.getByText('索引状态')).toBeInTheDocument();
    await user.click(screen.getByText('高级：连接与索引设置', { selector: 'summary' }));
    await user.click(screen.getByRole('combobox', { name: '向量模型服务' }));
    await user.click(await screen.findByRole('option', { name: 'OpenAI 兼容向量模型' }));
    await user.type(screen.getByRole('textbox', { name: '向量模型' }), 'bge-m3');
    await user.type(screen.getByRole('textbox', { name: '兼容 API 地址' }), 'https://embedding.example.test/v1');
    const dimensions = screen.getByRole('spinbutton', { name: '向量维度' });
    await user.clear(dimensions);
    await user.type(dimensions, '1024');
    await user.type(screen.getByRole('textbox', { name: '密钥环境变量名' }), 'PAW_EMBEDDING_API_KEY');
    await user.click(screen.getByRole('button', { name: '测试连接' }));

    expect(await screen.findByText('连接测试通过')).toBeInTheDocument();
    const probe = request(transport, 'knowledgeEmbedding.probe');
    expect(probe?.body).toMatchObject({ profile: {
      provider: 'openai-compatible', model: 'bge-m3', baseUrl: 'https://embedding.example.test/v1',
      dimensions: 1024, secretReference: 'PAW_EMBEDDING_API_KEY', denseBackend: 'sqlite-exact',
    } });
    expect(JSON.stringify(probe?.body)).not.toContain('secret-value');

    const embeddingWorkflow = (await screen.findAllByText('更新向量模型配置'))
      .map((element) => element.closest<HTMLElement>('.mgmt-workflow'))
      .find((workflow): workflow is HTMLElement => Boolean(workflow));
    if (!embeddingWorkflow) throw new Error('向量模型配置工作流未找到');
    expect(embeddingWorkflow).toHaveAttribute('data-confirmation', 'direct');
    await user.click(within(embeddingWorkflow).getByRole('button', { name: '更新向量模型配置' }));
    await waitFor(() => expect(request(transport, 'knowledgeEmbedding.impact')).toBeDefined());
    await waitFor(() => expect(request(transport, 'configuration.settings.preview')?.body).toMatchObject({ expectedRuntimeRevision: 12 }));

    await waitFor(() => expect(request(transport, 'configuration.settings.apply')?.body).toMatchObject({
      expectedRuntimeRevision: 12,
      previewToken: 'preview-embedding-settings',
      payloadSha256: 'sha256:embedding-settings',
      confirmText: 'apply',
      changes: {
        'knowledgeLibrary.embedding.provider': 'openai-compatible',
        'knowledgeLibrary.embedding.model': 'bge-m3',
        'knowledgeLibrary.embedding.secretReference': 'PAW_EMBEDDING_API_KEY',
      },
    }));
    expect(await within(embeddingWorkflow).findByText('已保存')).toBeInTheDocument();
    await user.click(within(embeddingWorkflow).getByRole('button', { name: '撤销' }));
    await waitFor(() => expect(request(transport, 'configuration.settings.rollback')?.body).toEqual({
      receiptId: 'receipt-embedding-settings',
      rollbackToken: 'rollback-embedding-settings',
      payloadSha256: 'sha256:embedding-settings',
      confirmText: 'rollback',
    }));
  });

  it('retries a failed document and confirms deletion without leaking storage paths', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    renderKnowledge(transport);

    expect((await screen.findAllByText('runtime.pdf')).length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: '交给 Trace Agent' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '重新解析 runtime.pdf' }));
    const reparseDialog = screen.getByRole('dialog', { name: '重新解析文档' });
    await user.click(within(reparseDialog).getByRole('combobox', { name: '解析方式' }));
    await user.click(await screen.findByRole('option', { name: 'MinerU OCR / 版面解析' }));
    await user.click(within(reparseDialog).getByRole('button', { name: '开始重新解析' }));
    await waitFor(() => expect(request(transport, 'knowledgeBases.document.retry')).toMatchObject({
      params: { kbId: 'kb-runtime', fileId: 'file-runtime' },
      body: { stage: 'parse', parserProvider: 'mineru_local_http', expectedRevision: 3 },
    }));
    await waitFor(() => expect(screen.getByRole('button', { name: '更多知识库工具' })).toHaveAttribute('data-current-tool', 'jobs'));
    await user.click(screen.getByRole('tab', { name: '资料' }));

    await user.click(screen.getByRole('button', { name: '删除 runtime.pdf' }));
    const dialog = screen.getByRole('dialog', { name: '删除文档' });
    expect(within(dialog).getByText(/runtime.pdf/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: '确认删除' }));
    await waitFor(() => expect(request(transport, 'knowledgeBases.document.delete')?.params).toEqual({ kbId: 'kb-runtime', fileId: 'file-runtime' }));
    expect(document.body).not.toHaveTextContent('/Users/private/Knowledge');
  });

  it('returns focus after dialogs and gives destructive confirmations danger emphasis', async () => {
    const user = userEvent.setup();
    const transport = createTransport();
    renderKnowledge(transport);

    const createTrigger = await screen.findByRole('button', { name: '新建知识库' });
    await user.click(createTrigger);
    await user.keyboard('{Escape}');
    await waitFor(() => expect(createTrigger).toHaveFocus());

    expect(screen.queryByRole('button', { name: '删除知识库' })).not.toBeInTheDocument();
    const deleteBaseTrigger = screen.getByRole('button', { name: '更多知识库工具' });
    await openKnowledgeTool(user, '删除知识库');
    const deleteBaseDialog = screen.getByRole('dialog', { name: '删除文档知识库' });
    expect(within(deleteBaseDialog).getByRole('button', { name: '确认删除' })).toHaveAttribute('data-variant', 'danger');
    expect(deleteBaseDialog).toContainElement(document.activeElement as HTMLElement);
    expect(transport.requests.some(({ request }) => request.pathId.startsWith('knowledgeBases.delete.'))).toBe(false);
    await user.keyboard('{Escape}');
    await waitFor(() => expect(deleteBaseTrigger).toHaveFocus());

    const reparseTrigger = screen.getByRole('button', { name: '重新解析 runtime.pdf' });
    await user.click(reparseTrigger);
    await user.keyboard('{Escape}');
    await waitFor(() => expect(reparseTrigger).toHaveFocus());

    const deleteDocumentTrigger = screen.getByRole('button', { name: '删除 runtime.pdf' });
    await user.click(deleteDocumentTrigger);
    const deleteDocumentDialog = screen.getByRole('dialog', { name: '删除文档' });
    expect(within(deleteDocumentDialog).getByRole('button', { name: '确认删除' })).toHaveAttribute('data-variant', 'danger');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(deleteDocumentTrigger).toHaveFocus());
  });

  it('keeps assembly history collapsed while reading and loads it only when expanded', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    renderKnowledge(transport, '/knowledge?tab=viewer', true);

    expect(await screen.findByRole('heading', { name: '伙伴工作循环' })).toBeVisible();
    const assembly = screen.getByText('装配记录', { selector: 'summary' });
    expect(assembly).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('region', { name: 'runtime.pdf 最近被哪些 Session 装配' })).not.toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.sessions.list')).toBe(false);

    await user.click(assembly);
    expect(await screen.findByRole('region', { name: 'runtime.pdf 最近被哪些 Session 装配' })).toBeVisible();
    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'agent.sessions.list')).toBe(true));
    expect(screen.getByRole('heading', { name: '伙伴工作循环' })).toBeVisible();

    await user.click(assembly);
    await waitFor(() => expect(screen.queryByRole('region', { name: 'runtime.pdf 最近被哪些 Session 装配' })).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: '返回资料' })).toBeVisible();
  });

  it('opens source, markdown, chunks, image and table artifacts from the file row', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    renderKnowledge(transport);

    await user.click(await screen.findByRole('button', { name: '查看 runtime.pdf' }));
    expect(await screen.findByRole('tab', { name: '源文件' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '正文' })).toHaveAttribute('data-state', 'active');
    expect(screen.getByRole('heading', { name: '伙伴工作循环' })).toBeInTheDocument();
    expect(screen.getByText('图片引用已隔离：远程图')).toBeInTheDocument();
    expect(screen.queryByRole('img', { name: '远程图' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('tab', { name: '源文件' }));
    expect(await screen.findByTitle('runtime.pdf 源文件')).toHaveAttribute('src', 'blob:knowledge-source');
    expect(transport.knowledgeDocumentSourceCalls[0]).toMatchObject({ kbId: 'kb-runtime', fileId: 'file-runtime' });
    await user.click(screen.getByRole('tab', { name: '段落' }));
    expect(screen.getByText('伙伴启动时注册 knowledge。')).toBeInTheDocument();
    expect(screen.getByText('伙伴工作循环 > 工具')).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: '解析产物' }));
    expect(await screen.findByRole('img', { name: '工具流程图' })).toHaveAttribute('src', 'blob:knowledge-asset');
    expect(screen.getByRole('link', { name: '查看 tool-flow.png' })).toHaveAttribute('href', 'blob:knowledge-asset');
    expect(screen.getByRole('link', { name: '下载 tool-flow.png' })).toHaveAttribute('download', 'tool-flow.png');
    expect(transport.knowledgeAssetCalls[0]).toMatchObject({ kbId: 'kb-runtime', fileId: 'file-runtime', assetId: 'd'.repeat(64) });
    expect(screen.getByRole('columnheader', { name: '字段' })).toBeInTheDocument();
    expect(screen.getByRole('cell', { name: 'knowledge' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '加载更多' }));
    expect(screen.getByRole('cell', { name: 'value-21' })).toBeInTheDocument();
  });

  it('keeps all RAG workspace tabs reachable at a narrow viewport', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 });
    window.dispatchEvent(new Event('resize'));
    const transport = createTransport();
    const user = userEvent.setup();
    renderKnowledge(transport);

    expect((await screen.findAllByText('runtime.pdf')).length).toBeGreaterThan(0);
    for (const name of ['查看材料', '搜索']) {
      await user.click(screen.getByRole('tab', { name }));
      expect(screen.getByRole('tab', { name })).toHaveAttribute('data-state', 'active');
    }
    await openKnowledgeTool(user, '处理记录');
    await openKnowledgeTool(user, '设置');
    expect(screen.getByRole('button', { name: '保存切分设置' })).toBeInTheDocument();
  });

  it('keeps document actions content-first at a narrow viewport', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 });
    window.dispatchEvent(new Event('resize'));
    const transport = createTransport();
    renderKnowledge(transport);

    expect(await screen.findByRole('button', { name: '重新解析 runtime.pdf' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '查看 runtime.pdf' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '删除 runtime.pdf' })).toBeInTheDocument();
  });

  it('paginates Markdown and chunks without hiding content after the first window', async () => {
    const transport = createTransport({ pagedDetail: true });
    const user = userEvent.setup();
    renderKnowledge(transport);

    await user.click(await screen.findByRole('button', { name: '查看 runtime.pdf' }));
    expect(await screen.findByRole('heading', { name: '第一段' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '继续加载正文' }));
    expect(await screen.findByRole('heading', { name: '第二段' })).toBeInTheDocument();

    await user.click(screen.getByRole('tab', { name: '段落' }));
    expect(screen.getByText('第一个片段')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '加载更多' }));
    expect(await screen.findByText('第二个片段')).toBeInTheDocument();
    expect(screen.getByText('已加载全部 2 个段落。')).toBeInTheDocument();
  });

  it('renders the reading outline and scrolls to the picked heading', async () => {
    const scrollIntoView = vi.spyOn(window.HTMLElement.prototype, 'scrollIntoView').mockImplementation(() => undefined);
    const transport = createTransport();
    const user = userEvent.setup();
    renderKnowledge(transport);

    await user.click(await screen.findByRole('button', { name: '查看 runtime.pdf' }));
    const outline = await screen.findByRole('navigation', { name: '文档目录' });
    expect(within(outline).getByText('1 个标题')).toBeInTheDocument();
    const entry = within(outline).getByRole('button', { name: '伙伴工作循环' });
    await user.click(entry);
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    expect(entry).toHaveAttribute('aria-current', 'location');
  });

  it('keeps the outline honest about partially loaded content', async () => {
    const transport = createTransport({ pagedDetail: true });
    const user = userEvent.setup();
    renderKnowledge(transport);

    await user.click(await screen.findByRole('button', { name: '查看 runtime.pdf' }));
    const outline = await screen.findByRole('navigation', { name: '文档目录' });
    expect(within(outline).getByText('目录来自已加载的 2 / 4 行，继续加载正文后会补全。')).toBeInTheDocument();
    expect(within(outline).queryByRole('button', { name: '第二段' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '继续加载正文' }));
    expect(await within(outline).findByRole('button', { name: '第二段' })).toBeInTheDocument();
    expect(within(outline).queryByText(/目录来自已加载/)).not.toBeInTheDocument();
  });

  it('caps a drop at 20 files and says how many were left out', async () => {
    const transport = createTransport();
    Object.defineProperty(transport, 'kind', { value: 'http' });
    renderKnowledge(transport);

    expect((await screen.findAllByText('runtime.pdf')).length).toBeGreaterThan(0);
    const files = Array.from({ length: 22 }, (_, index) => new File(['x'], `note-${index + 1}.md`, { type: 'text/markdown' }));
    fireEvent.drop(screen.getByRole('button', { name: '导入文件' }), { dataTransfer: { files } });

    expect(await screen.findByText('一次最多导入 20 个文件：已开始前 20 个，其余 2 个请分批拖入。')).toBeInTheDocument();
    await waitFor(() => expect(transport.knowledgeImportCalls).toHaveLength(20));
    expect(transport.knowledgeImportCalls.every((call) => call.maxFiles === 1)).toBe(true);
    expect(transport.knowledgeImportCalls.flatMap((call) => call.files?.map((file) => file.name) ?? [])).not.toContain('note-21.md');
  });

  it('says when a drop contains no files instead of ignoring it', async () => {
    const transport = createTransport();
    Object.defineProperty(transport, 'kind', { value: 'http' });
    renderKnowledge(transport);

    expect((await screen.findAllByText('runtime.pdf')).length).toBeGreaterThan(0);
    fireEvent.drop(screen.getByRole('button', { name: '导入文件' }), { dataTransfer: { files: [] } });

    expect(await screen.findByText('拖入的内容里没有文件；请直接拖动本机文件，或点击导入区选择。')).toBeInTheDocument();
    expect(transport.knowledgeImportCalls).toHaveLength(0);
  });

  it('maps succeeded jobs to completed and exposes task details', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    renderKnowledge(transport);

    await openKnowledgeTool(user, '处理记录');
    expect(screen.getAllByText('已完成').length).toBeGreaterThan(0);
    expect(screen.queryByText('处理中')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /runtime.pdf/ }));
    expect(screen.queryByText('job-1')).not.toBeInTheDocument();
    await user.click(screen.getByText('高级：处理详情', { selector: 'summary' }));
    expect(screen.getByText('job-1')).toBeInTheDocument();
    expect(screen.getByRole('list', { name: '任务阶段记录' })).toBeInTheDocument();
  });

  it('previews all server chunking strategies without writing the index', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    renderKnowledge(transport);

    await openKnowledgeTool(user, '设置');
    const strategy = screen.getByRole('combobox', { name: '策略' });
    await user.click(strategy);
    expect(await screen.findAllByRole('option')).toHaveLength(7);
    await user.click(screen.getByRole('option', { name: '法律条款' }));
    await user.click(screen.getByRole('button', { name: '预览切分' }));

    await waitFor(() => expect(request(transport, 'knowledgeBases.chunkPreview')).toMatchObject({
      params: { kbId: 'kb-runtime', fileId: 'file-runtime' },
      body: { chunkingConfig: { strategy: 'laws' }, limit: 12 },
    }));
    expect(await screen.findByText('2 个段落')).toBeInTheDocument();
    expect(screen.getByText(/\u4f19伴运行环境/)).toBeInTheDocument();
    expect(screen.getByText(/\u5de5具只按需检索/)).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent('Agent Runtime');
  });

  it('cancels an active indexing job from the jobs workspace', async () => {
    const transport = createTransport({ activeJob: true });
    const user = userEvent.setup();
    renderKnowledge(transport);

    await openKnowledgeTool(user, '处理记录');
    await user.click(screen.getByRole('button', { name: '取消任务' }));

    await waitFor(() => expect(request(transport, 'knowledgeBases.job.cancel')).toMatchObject({
      params: { kbId: 'kb-runtime', jobId: 'job-1' },
      body: {},
    }));
  });

  it('keeps failed uploads in the queue and retries them visibly', async () => {
    const transport = createTransport();
    vi.spyOn(transport, 'importKnowledgeDocuments')
      .mockRejectedValueOnce(new Error('上传连接中断'))
      .mockResolvedValueOnce([{ kbId: 'kb-runtime', documentId: 'file-retry', fileName: 'retry.pdf', mimeType: 'application/pdf', byteSize: 100, sha256: 'e'.repeat(64), status: 'queued' }]);
    const user = userEvent.setup();
    renderKnowledge(transport);

    await user.click(await screen.findByRole('button', { name: '导入文件' }));
    expect((await screen.findAllByText('上传连接中断')).length).toBeGreaterThan(0);
    await user.click(screen.getByRole('button', { name: '重试上传 本机文件导入' }));
    expect(await screen.findByText('retry.pdf')).toBeInTheDocument();
    expect(screen.getByText('已进入解析')).toBeInTheDocument();
  });

  it('imports dropped files directly when the transport carries raw uploads', async () => {
    const transport = createTransport();
    Object.defineProperty(transport, 'kind', { value: 'http' });
    renderKnowledge(transport);

    expect((await screen.findAllByText('runtime.pdf')).length).toBeGreaterThan(0);
    const dropped = new File(['# note'], 'dropped.md', { type: 'text/markdown' });
    fireEvent.drop(screen.getByRole('button', { name: '导入文件' }), { dataTransfer: { files: [dropped] } });

    await waitFor(() => expect(transport.knowledgeImportCalls).toHaveLength(1));
    expect(transport.knowledgeImportCalls[0]).toMatchObject({ kbId: 'kb-runtime', parserProvider: 'auto', maxFiles: 1 });
    expect(transport.knowledgeImportCalls[0]?.files?.map((file) => file.name)).toEqual(['dropped.md']);
    expect(await screen.findByRole('region', { name: '上传队列' })).toHaveTextContent('dropped.md');
  });

  it('explains dropped files need the web transport instead of failing silently', async () => {
    const transport = createTransport();
    renderKnowledge(transport);

    expect((await screen.findAllByText('runtime.pdf')).length).toBeGreaterThan(0);
    fireEvent.drop(screen.getByRole('button', { name: '导入文件' }), { dataTransfer: { files: [new File(['x'], 'x.md', { type: 'text/markdown' })] } });

    expect(await screen.findByText('当前运行环境不支持拖放导入；请点击导入区改用系统文件选择。')).toBeInTheDocument();
    expect(transport.knowledgeImportCalls).toHaveLength(0);
  });

  it('filters the material list by name and recovers from empty matches', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    renderKnowledge(transport);

    expect((await screen.findAllByText('runtime.pdf')).length).toBeGreaterThan(0);
    const filter = screen.getByRole('textbox', { name: '筛选文件' });
    await user.type(filter, '不存在的名字');
    expect(await screen.findByText('没有匹配的文件')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '显示全部文件' }));
    expect((await screen.findAllByText('runtime.pdf')).length).toBeGreaterThan(0);
    await user.type(filter, 'RUNTIME');
    expect(await screen.findByText(/1 \/ /)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '清除筛选' }));
    expect(filter).toHaveValue('');
  });

  it('returns from the viewer to the materials workspace in one step', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    renderKnowledge(transport);

    expect((await screen.findAllByText('runtime.pdf')).length).toBeGreaterThan(0);
    await user.click(screen.getByRole('button', { name: '查看 runtime.pdf' }));
    await waitFor(() => expect(screen.getByRole('tab', { name: '查看材料' })).toHaveAttribute('data-state', 'active'));
    await user.click(screen.getByRole('button', { name: '返回资料' }));
    expect(screen.getByRole('tab', { name: '资料' })).toHaveAttribute('data-state', 'active');
  });

  it('manages an independent document graph with source navigation and rebuild state', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    const desktopRoot = document.createElement('div');
    desktopRoot.className = 'paw-desktop-root';
    desktopRoot.dataset.testPawDesktopRoot = 'true';
    document.body.append(desktopRoot);
    renderKnowledge(transport);

    await openKnowledgeTool(user, '知识图谱');
    expect(await screen.findByText('4')).toBeInTheDocument();
    expect(request(transport, 'knowledgeBases.graph.get')?.query).toMatchObject({ limit: 80, depth: 2, excludeChunks: true });
    expect(screen.getByLabelText('交互式知识图谱画布')).toHaveClass('knowledge-graph__canvas');
    expect(screen.getByLabelText('交互式知识图谱画布')).toHaveAttribute('data-renderer', 'g6');
    expect(screen.getByLabelText('交互式知识图谱画布')).toHaveAttribute('data-layout', 'force-network');
    expect(screen.getByLabelText('交互式知识图谱画布')).toHaveAttribute('data-edge-mode', 'semantic');
    await user.click(screen.getByRole('switch', { name: '显示结构关系' }));
    expect(screen.getByLabelText('交互式知识图谱画布')).toHaveAttribute('data-edge-mode', 'structure');
    expect(screen.getByRole('button', { name: '适应画布' })).toHaveAttribute('title', '图谱正在加载');
    expect(screen.getByRole('button', { name: '定位节点' })).toHaveAttribute('title', '请先选择一个节点');
    await user.click(screen.getByRole('button', { name: '专注查看' }));
    const focusedGraph = screen.getByLabelText('知识图谱工作区');
    expect(focusedGraph).toHaveAttribute('data-focus', 'true');
    expect(focusedGraph.parentElement).toBe(desktopRoot);
    expect(screen.getByRole('button', { name: '返回知识库' })).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: '节点上限' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '返回知识库' }));
    expect(screen.getByLabelText('知识图谱工作区')).not.toHaveAttribute('data-focus');
    expect(screen.getByRole('combobox', { name: '节点上限' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '专注查看' }));
    await user.keyboard('{Escape}');
    expect(screen.getByLabelText('知识图谱工作区')).not.toHaveAttribute('data-focus');
    expect(screen.getByRole('combobox', { name: '节点上限' })).toBeInTheDocument();
    await user.type(screen.getByRole('textbox', { name: '搜索图谱' }), 'DeepSeek');
    await waitFor(() => expect(transport.requests.filter((call) => call.request.pathId === 'knowledgeBases.graph.get' && call.request.query?.query === 'DeepSeek')).toHaveLength(1), { timeout: 1_500 });
    expect(transport.requests.filter((call) => call.request.pathId === 'knowledgeBases.graph.get' && call.request.query?.query)).toHaveLength(1);
    await user.click(screen.getByRole('radio', { name: '节点' }));
    expect(screen.getByRole('button', { name: /知识整理服务/ })).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent('Knowledge Worker');
    await user.click(screen.getByRole('button', { name: /按需检索与上下文注入/ }));
    expect(screen.getByLabelText('节点详情')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '按需检索与上下文注入' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '打开材料来源' }));
    expect(screen.getByRole('tab', { name: '查看材料' })).toHaveAttribute('data-state', 'active');

    await openKnowledgeTool(user, '知识图谱');
    await user.click(screen.getByRole('radio', { name: '关系' }));
    expect(document.body).not.toHaveTextContent('contains');
    expect(document.body).not.toHaveTextContent('mentions');
    await user.click(screen.getByRole('button', { name: /runtime.pdf → 工具注册/ }));
    expect(screen.getByLabelText('关系详情')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '包含' })).toBeInTheDocument();
    expect(screen.getByText('类型').nextElementSibling).toHaveTextContent('包含');
    await user.click(screen.getByRole('button', { name: '关闭图谱详情' }));
    expect(screen.queryByLabelText('关系详情')).not.toBeInTheDocument();
    await user.click(screen.getByRole('radio', { name: '构建状态' }));
    expect(screen.getByText('图谱从当前文档知识库构建，用于管理，并在回答时提供参考；它不与个人记忆图谱合并。')).toBeInTheDocument();
    expect(screen.getByText('已索引材料')).toBeInTheDocument();
    expect(screen.getByText('待处理材料')).toBeInTheDocument();
    expect(screen.getByText('下次重建').nextElementSibling).toHaveTextContent('模型整理（推荐）');
    expect(screen.queryByText('由当前配置决定')).not.toBeInTheDocument();
    await user.click(screen.getByText('高级：构建详情', { selector: 'summary' }));
    expect(screen.getByText('由当前配置决定')).toBeInTheDocument();
    expect(screen.getByText('抽取上限').nextElementSibling).toHaveTextContent('5 实体 / 4 关系 / 2 主题');
    await user.click(screen.getAllByRole('button', { name: '重建图谱' }).at(-1)!);
    await waitFor(() => expect(request(transport, 'knowledgeBases.graph.rebuild')?.body).toEqual({
      expectedRevision: 4,
      extractorMode: 'model',
      batchSize: 4,
      extractionConcurrency: 2,
      maxEntitiesPerChunk: 5,
      maxRelationsPerChunk: 4,
      maxTopicsPerChunk: 2,
    }));
  });

  it('states the graph evidence boundary and reveals every remaining relation', async () => {
    const user = userEvent.setup();
    renderKnowledge(createTransport({ manyRelations: true }));

    await openKnowledgeTool(user, '知识图谱');
    await user.click(await screen.findByRole('radio', { name: '节点' }));
    await user.click(screen.getByRole('button', { name: /按需检索与上下文注入/ }));

    const inspector = screen.getByLabelText('节点详情');
    expect(within(inspector).getByText('显示 8 / 共 11 条')).toBeVisible();
    expect(within(inspector).queryByText('关系节点9')).not.toBeInTheDocument();
    await user.click(within(inspector).getByText('查看其余 3 条关系'));
    expect(within(inspector).getByText('关系节点9')).toBeVisible();
  });

  it('keeps build status reachable before the first graph has nodes', async () => {
    const transport = createTransport({ emptyGraph: true });
    const user = userEvent.setup();
    renderKnowledge(transport);
    await openKnowledgeTool(user, '知识图谱');
    expect(await screen.findByRole('heading', { name: '当前范围没有图谱节点' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '查看构建状态' }));
    expect(screen.getByText('图谱构建状态')).toBeInTheDocument();
    expect(screen.getByText('待处理材料')).toBeInTheDocument();
  });

  it('polls a queued model graph until ready and prevents duplicate rebuilds', async () => {
    const transport = createTransport({ pollingGraph: true });
    const user = userEvent.setup();
    renderKnowledge(transport);

    await openKnowledgeTool(user, '知识图谱');
    await user.click(screen.getByRole('radio', { name: '构建状态' }));
    expect(screen.getByRole('heading', { name: '构建中' })).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: '重建图谱' })).toHaveLength(2);
    expect(screen.getAllByRole('button', { name: '重建图谱' }).every((button) => button.hasAttribute('disabled'))).toBe(true);

    await waitFor(() => expect(transport.requests.filter((call) => call.request.pathId === 'knowledgeBases.graph.get')).toHaveLength(2), { timeout: 2_500 });
    await waitFor(() => expect(screen.getAllByRole('button', { name: '重建图谱' }).every((button) => !button.hasAttribute('disabled'))).toBe(true));
    expect(screen.getByRole('heading', { name: '已就绪' })).toBeInTheDocument();
  });
  it('keeps the Settings workspace and unsaved drafts through an authoritative refresh', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    renderKnowledge(transport, '/knowledge?tab=settings');

    expect(await screen.findByRole('button', { name: '更多知识库工具' })).toHaveAttribute('data-current-tool', 'settings');
    const name = screen.getByRole('textbox', { name: '名称' });
    await user.clear(name);
    await user.type(name, '尚未保存的知识库名称');
    await user.click(screen.getByRole('button', { name: '刷新知识库' }));

    await waitFor(() => expect(screen.getByRole('button', { name: '更多知识库工具' })).toHaveAttribute('data-current-tool', 'settings'));
    expect(screen.getByRole('textbox', { name: '名称' })).toHaveValue('尚未保存的知识库名称');
  });

  it('keeps create and basic-info drafts visible when the backend does not confirm them', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    renderKnowledge(transport);

    await user.click(await screen.findByRole('button', { name: '新建知识库' }));
    const dialog = screen.getByRole('dialog', { name: '新建文档知识库' });
    await user.type(within(dialog).getByRole('textbox', { name: '名称' }), '没有被确认的新库');
    await user.click(within(dialog).getByRole('button', { name: '创建' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('知识服务没有确认新知识库');
    expect(dialog).toBeInTheDocument();

    await user.click(within(dialog).getByRole('button', { name: '取消' }));
    await openKnowledgeTool(user, '设置');
    const name = screen.getByRole('textbox', { name: '名称' });
    await user.clear(name);
    await user.type(name, '没有被确认的名称');
    await user.click(screen.getByRole('button', { name: '保存基本信息' }));
    expect(await screen.findByText('知识服务没有确认基本信息更新，页面仍保留你的输入。')).toBeInTheDocument();
    expect(name).toHaveValue('没有被确认的名称');
  });

  it('shows the chunking draft as current → proposed and discards it without saving', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    renderKnowledge(transport, '/knowledge?tab=settings');

    const chunkSize = await screen.findByRole('spinbutton', { name: '大小' });
    await user.clear(chunkSize);
    await user.type(chunkSize, '1400');

    const draft = screen.getByText('未保存的更改 · 1 项').closest('.knowledge-settings__draft');
    if (!(draft instanceof HTMLElement)) throw new Error('切分草稿区块未找到');
    expect(within(draft).getByText('当前 → 保存后')).toBeInTheDocument();
    expect(within(draft).getByText('大小')).toBeInTheDocument();
    expect(within(draft).getByText('1200')).toBeInTheDocument();
    expect(within(draft).getByText('1400')).toBeInTheDocument();
    expect(within(draft).getByText('保存后，新导入的材料按新切分处理；已有材料进入待重建，重建完成前检索仍使用现有段落。')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '放弃切分更改' }));
    expect(screen.queryByText(/未保存的更改/)).not.toBeInTheDocument();
    expect(screen.getByRole('spinbutton', { name: '大小' })).toHaveValue(1200);
    expect(screen.getByRole('button', { name: '保存切分设置' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: '放弃切分更改' })).not.toBeInTheDocument();
    expect(transport.requests.some((call) => call.request.pathId === 'knowledgeBases.update')).toBe(false);
  });

  it('shows a cleared basic-info field as 未填写 in the draft and keeps save honest', async () => {
    const user = userEvent.setup();
    renderKnowledge(createTransport(), '/knowledge?tab=settings');

    const description = await screen.findByRole('textbox', { name: '说明' });
    await user.clear(description);
    const draft = screen.getByText('未保存的更改 · 1 项').closest('.knowledge-settings__draft');
    if (!(draft instanceof HTMLElement)) throw new Error('基本信息草稿区块未找到');
    expect(within(draft).getByText('说明')).toBeInTheDocument();
    expect(within(draft).getByText('只包含外部文档')).toBeInTheDocument();
    expect(within(draft).getByText('（未填写）')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存基本信息' })).toBeEnabled();

    await user.click(screen.getByRole('button', { name: '放弃基本信息更改' }));
    expect(screen.queryByText(/未保存的更改/)).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '说明' })).toHaveValue('只包含外部文档');
    expect(screen.getByRole('button', { name: '保存基本信息' })).toBeDisabled();
  });

  it('keeps materials stats on library-level truth and marks the selected file row', async () => {
    renderKnowledge(createTransport());

    expect((await screen.findAllByText('runtime.pdf')).length).toBeGreaterThan(0);
    expect(screen.getByText('已索引段落').nextElementSibling).toHaveTextContent('0');
    expect(screen.queryByText('提取内容')).not.toBeInTheDocument();
    const selectedRow = screen.getByRole('button', { current: true });
    expect(selectedRow).toHaveClass('knowledge-material-row__select');
    expect(selectedRow).toHaveTextContent('runtime.pdf');
  });

  it('opens document row action tips above the row so they do not cover adjacent files', async () => {
    const user = userEvent.setup();
    renderKnowledge(createTransport());

    const view = await screen.findByRole('button', { name: '查看 runtime.pdf' });
    await user.hover(view);
    const tooltip = await screen.findByRole('tooltip', { name: '查看 runtime.pdf' });
    expect(tooltip.parentElement).toHaveAttribute('data-side', 'top');
  });

});

function renderKnowledge(transport: MockControlTransport, initialEntry = '/knowledge?tab=materials', pawOs = false, active = true, remountOnRoute = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const feature = <QueryClientProvider client={client}>{remountOnRoute ? <RouteRemountedKnowledge /> : <KnowledgeFeature />}</QueryClientProvider>;
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <TooltipProvider delayDuration={0}>
        <ControlTransportProvider transport={transport}>
          {pawOs ? <PawOsAppSurfaceProvider active={active} appId="knowledge" height={760} width={1_200}>{feature}</PawOsAppSurfaceProvider> : feature}
        </ControlTransportProvider>
      </TooltipProvider>
    </MemoryRouter>,
  );
}

async function openKnowledgeTool(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(await screen.findByRole('button', { name: '更多知识库工具' }));
  await user.click(await screen.findByRole('menuitem', { name }));
}

function RouteRemountedKnowledge() {
  const location = useLocation();
  return <KnowledgeFeature key={location.search} />;
}

function createTransport(options: { activeJob?: boolean; emptyGraph?: boolean; manyRelations?: boolean; pagedDetail?: boolean; pollingGraph?: boolean; multipleSearchHits?: boolean; unlistedSearchSource?: boolean } = {}): MockControlTransport {
  let graphRequestCount = 0;
  const extraGraphNodes = options.manyRelations
    ? Array.from({ length: 9 }, (_, index) => ({
      id: `related-${index + 1}`,
      label: `关系节点${index + 1}`,
      kind: 'topic',
      weight: .5,
    }))
    : [];
  const extraGraphEdges = options.manyRelations
    ? Array.from({ length: 9 }, (_, index) => ({
      id: `edge-extra-${index + 1}`,
      source: 'chunk-tool',
      target: `related-${index + 1}`,
      kind: 'related',
      label: '关联',
      weight: .5,
    }))
    : [];
  return new MockControlTransport({
    capabilities: { features: { managementWorkContract: true, configurationSettingsWorkContract: true } },
    knowledgeAsset: (input) => ({ ...input, mimeType: 'image/png', byteSize: 3, sha256: input.assetId, blob: new Blob(['png'], { type: 'image/png' }) }),
    knowledgeDocumentSource: (input) => ({ ...input, mimeType: 'application/pdf', byteSize: 3, sha256: 'b'.repeat(64), blob: new Blob(['pdf'], { type: 'application/pdf' }) }),
    knowledgeImportReceipts: [{
      kbId: 'kb-runtime', documentId: 'file-new', fileName: 'new.pdf', mimeType: 'application/pdf',
      byteSize: 2_048, sha256: 'a'.repeat(64), status: 'queued',
    }],
    routes: {
      'knowledgeBases.list': { items: [knowledgeBase()] },
      'knowledgeBases.get': { base: knowledgeBase() },
      'knowledgeBases.documents.list': { items: [options.unlistedSearchSource ? { ...knowledgeDocument(), id: 'file-unrelated', name: 'unrelated.pdf' } : knowledgeDocument()] },
      'knowledgeBases.jobs.list': { items: [{ id: 'job-1', fileId: 'file-runtime', fileName: 'runtime.pdf', kind: 'reindex', parserMode: 'builtin', status: options.activeJob ? 'running' : 'succeeded', stage: options.activeJob ? 'indexing' : 'ready', progress: options.activeJob ? .8 : 1, cancellable: options.activeJob, revision: 3, createdAtMs: Date.now() - 2_000, startedAtMs: Date.now() - 1_500, finishedAtMs: options.activeJob ? 0 : Date.now() - 500, updatedAtMs: Date.now() - 500 }] },
      'knowledgeWorker.health': { ok: true, status: 'ready', dense: { available: true, degraded: false, kind: 'sqlite-vector-projection', fingerprint: 'local-hash:96:v1', vectorCount: 42 } },
      'knowledgeParsers.list': { items: [{ id: 'mineru_local_http', enabled: true, ready: true, status: 'ready' }] },
      'knowledgeEmbedding.profile': {
        ok: true,
        profile: { source: 'settings', provider: 'local-hash', model: 'deterministic-term-vector-v1', baseUrl: '', dimensions: 96, secretReference: '', queryPrefix: '', documentPrefix: '', denseBackend: 'sqlite-exact', secretAvailable: false, profileSha256: 'profile-current', secretsVisible: false },
        phase: 'active',
        runtime: { provider: { provider: 'local-hash', model: 'deterministic-term-vector-v1' }, fingerprint: 'local-hash:96:v1', dimensions: 96, vectorCount: 42, chunkCount: 42, coverage: 1, available: true, degraded: true, reason: 'local baseline' },
        secretsVisible: false,
      },
      'knowledgeEmbedding.probe': (request: ControlRequest) => {
        const profile = (request.body as unknown as { profile: Record<string, unknown> }).profile;
        return { ok: true, ready: true, profileSha256: 'profile-candidate', provider: profile.provider, model: profile.model, fingerprint: 'openai-compatible:bge-m3:test', dimensions: profile.dimensions, semantic: true, latencyMs: 12.5, secretsVisible: false };
      },
      'knowledgeEmbedding.impact': (request: ControlRequest) => {
        const candidate = (request.body as unknown as { profile: Record<string, unknown> }).profile;
        return {
          ok: true, candidate,
          probe: { ready: true, profileSha256: 'profile-candidate', provider: candidate.provider, model: candidate.model, fingerprint: 'openai-compatible:bge-m3:test', dimensions: candidate.dimensions, semantic: true, latencyMs: 12.5, secretsVisible: false },
          currentProfileSha256: 'profile-current',
          configurationChanges: Object.fromEntries(Object.entries(candidate).map(([key, value]) => [`knowledgeLibrary.embedding.${key}`, value])),
          requiresWorkerRestart: true, requiresRebuild: true,
          affectedBases: [{ kbId: 'kb-runtime', name: '伙伴运行资料', documentCount: 1, chunkCount: 42 }],
          affectedBaseCount: 1, affectedDocumentCount: 1, affectedChunkCount: 42,
          approvalRequiredForApply: true, secretsVisible: false,
        };
      },
      'configuration.settings': { ok: true, runtimeRevision: 12, settings: {} },
      'configuration.settings.preview': {
        ok: true, pathId: 'configuration.settings.apply', previewToken: 'preview-embedding-settings', payloadSha256: 'sha256:embedding-settings', requiredConfirm: 'apply', expiresAtMs: Date.now() + 60_000,
        expectedRevision: { runtimeRevision: 12 }, summary: { title: '应用设置', items: ['更新 Embedding Profile'], risk: 'R2' },
      },
      'configuration.settings.apply': {
        ok: true, pathId: 'configuration.settings.apply', receiptId: 'receipt-embedding-settings', payloadSha256: 'sha256:embedding-settings', appliedAtMs: Date.now(), rollbackAvailable: true, rollbackToken: 'rollback-embedding-settings',
      },
      'configuration.settings.rollback': {
        ok: true, pathId: 'configuration.settings.rollback', receiptId: 'receipt-embedding-rollback', payloadSha256: 'sha256:embedding-settings', appliedAtMs: Date.now(), rollbackAvailable: false, rollbackToken: '',
      },
      'knowledgeBases.search': {
        retrieval: {
          mode: 'hybrid', effectiveMode: 'hybrid',
          config: { mode: 'hybrid', topK: 10, threshold: .2, lexicalWeight: 1, denseWeight: 1, graphEnabled: true, graphWeight: .7, rrfK: 60, candidateMultiplier: 4, rerankEnabled: false, rerankCandidateDepth: 40 },
          libraries: [{ kbId: 'kb-runtime', kbName: '伙伴运行资料', config: { mode: 'hybrid', topK: 10, threshold: .2, lexicalWeight: 1, denseWeight: 1, graphEnabled: true, graphWeight: .7, rrfK: 60, candidateMultiplier: 4, rerankEnabled: false, rerankCandidateDepth: 40 }, effectiveMode: 'hybrid', candidateLimit: 40, lexicalCandidates: 2, denseCandidates: 2, graphCandidates: 1, graphStatus: 'ready', graphMatchedNodes: 2, rerankApplied: false, rerankCandidates: 0, returned: 1 }],
          lexicalAvailable: true, dense: { available: true, degraded: false, provider: 'local-hash', model: 'test' },
          reranker: { provider: 'none', configured: false, independentStage: true, subagentSubstitute: false, fallbackCount: 0 },
        },
        hits: [...(options.multipleSearchHits ? [{
          id: 'chunk-intro', documentId: 'file-runtime', documentName: 'runtime.pdf', title: '概述',
          excerpt: '运行时概述。', score: .95, page: 1, heading: '简介',
        }] : []), {
          id: 'chunk-tool', documentId: 'file-runtime', documentName: 'runtime.pdf', title: 'Tool 注册',
          excerpt: 'Agent 启动时注册 knowledge。', score: .92, page: 12, heading: 'Agent Loop > Tools',
          diagnostics: {
            effectiveMode: 'hybrid', lexicalRank: 1, denseRank: 2, graphRank: 1,
            lexicalScore: .95, denseScore: .88, graphScore: .9,
            graphMatches: ['Tool', 'Knowledge Worker'],
            graphPaths: [
              'Tool → mentions → 文档片段',
              '文档片段 → evidence → 工具注册',
              '知识整理服务 → uses → 检索器',
              '检索器 → reads → runtime.pdf',
            ],
          },
        }],
      },
      'knowledgeBases.document.get': options.pagedDetail ? pagedKnowledgeDetail : (request: ControlRequest) => ({
        ...knowledgeDetail(),
        ...(request.params?.fileId === 'file-unrelated' ? { document: { ...knowledgeDocument(), id: 'file-unrelated', name: 'unrelated.pdf' } } : {}),
      }),
      'knowledgeBases.open': { ok: true, content: '工具原文' },
      'knowledgeBases.update': { base: knowledgeBase() },
      'knowledgeBases.create': { base: knowledgeBase() },
      'knowledgeBases.document.retry': { ok: true },
      'knowledgeBases.document.delete': { ok: true },
      'knowledgeBases.reindexPreview': { previewToken: 'preview-reindex', payloadSha256: 'sha256:reindex', expectedRevision: 8, summary: { documentCount: 1, staleDocumentCount: 1, estimatedChunkCount: 48 } },
      'knowledgeBases.rebuild': { ok: true },
      'knowledgeBases.job.cancel': { ok: true, job: { id: 'job-1', status: 'cancelled' } },
      'knowledgeBases.chunkPreview': { ok: true, fileId: 'file-runtime', total: 2, truncated: false, items: [{ chunkId: 'preview-1', ordinal: 0, content: '# Agent Runtime\nTool 只按需检索。', page: 1 }, { chunkId: 'preview-2', ordinal: 1, content: '第二条预览', page: 2 }] },
      'knowledgeBases.graph.get': () => {
        graphRequestCount += 1;
        return {
        schemaVersion: 'rag-ime.knowledge-graph.v1', kbId: 'kb-runtime', revision: 4,
        sourceRevision: `sha256:${'b'.repeat(64)}`,
        status: options.emptyGraph ? 'stale' : options.pollingGraph && graphRequestCount === 1 ? 'building' : 'ready', updatedAtMs: Date.now(),
        nodes: options.emptyGraph ? [] : [
          { id: 'doc-runtime', label: 'runtime.pdf', kind: 'document', documentId: 'file-runtime', documentName: 'runtime.pdf', weight: 1 },
          { id: 'topic-tools', label: '工具注册', kind: 'topic', weight: .9 },
          { id: 'entity-worker', label: 'Knowledge Worker', kind: 'entity', weight: .8 },
          { id: 'chunk-tool', label: '按需检索与上下文注入', kind: 'chunk', documentId: 'file-runtime', documentName: 'runtime.pdf', chunkId: 'chunk-tool', heading: 'Agent Loop > Tools', excerpt: 'Agent 启动时注册 knowledge。', page: 12, weight: .92 },
          ...extraGraphNodes,
        ],
        edges: options.emptyGraph ? [] : [
          { id: 'edge-1', source: 'doc-runtime', target: 'topic-tools', kind: 'contains', label: '包含', weight: .9 },
          { id: 'edge-2', source: 'topic-tools', target: 'chunk-tool', kind: 'evidence', label: '证据', weight: .92 },
          { id: 'edge-3', source: 'chunk-tool', target: 'entity-worker', kind: 'mentions', label: '提及', weight: .8 },
          ...extraGraphEdges,
        ],
        stats: {
          nodeCount: options.emptyGraph ? 0 : 4 + extraGraphNodes.length,
          edgeCount: options.emptyGraph ? 0 : 3 + extraGraphEdges.length,
          documentCount: options.emptyGraph ? 0 : 1,
          chunkCount: options.emptyGraph ? 0 : 1,
          indexedDocumentCount: options.emptyGraph ? 0 : 1,
          pendingDocumentCount: options.emptyGraph ? 1 : 0,
        },
        truncated: false,
      }; },
      'knowledgeBases.graph.rebuild': { ok: true, jobId: 'graph-job-1', status: 'queued' },
    },
  });
}

function knowledgeBase() {
  return {
    id: 'kb-runtime', name: '伙伴运行资料', description: '只包含外部文档', documentCount: 1,
    chunkCount: 42, status: 'ready', agentEnabled: false, parserProvider: 'auto', updatedAtMs: Date.now(), revision: 8,
    chunkingConfig: { strategy: 'markdown', size: 1_200, overlap: 160, separator: '\n\n', respectHeadings: true, respectPageBoundaries: true },
    retrievalConfig: {
      mode: 'hybrid', topK: 10, threshold: .2, lexicalWeight: 1, denseWeight: 1,
      graphEnabled: true, graphWeight: .7, rrfK: 60, candidateMultiplier: 4, rerankEnabled: false, rerankCandidateDepth: 40,
    },
  };
}

function knowledgeDocument() {
  return {
    id: 'file-runtime', baseId: 'kb-runtime', name: 'runtime.pdf', mimeType: 'application/pdf', byteSize: 4_096,
    status: 'failed', stage: 'parse', progress: .4, error: '扫描文本质量不足', chunkCount: 0, parserProvider: 'builtin',
    updatedAtMs: Date.now(), revision: 3, sha256: 'b'.repeat(64), pageCount: 12, tokenCount: 3_200,
    parserVersion: 'builtin-1', indexedConfigRevision: 8, sourceReadPath: '/api/knowledge-bases/kb-runtime/documents/file-runtime/source',
  };
}

function pagedKnowledgeDetail(request: ControlRequest) {
  const chunkOffset = Number(request.query?.offset ?? 0);
  const lineOffset = Number(request.query?.lineOffset ?? 0);
  const contentRequest = Number(request.query?.lineLimit ?? 0) === 200;
  const chunks = contentRequest
    ? [{ chunkId: 'chunk-1', ordinal: 0, content: '第一个片段', page: 1, tokenCount: 4 }]
    : chunkOffset === 0
      ? [{ chunkId: 'chunk-1', ordinal: 0, content: '第一个片段', page: 1, tokenCount: 4 }]
      : [{ chunkId: 'chunk-2', ordinal: 1, content: '第二个片段', page: 2, tokenCount: 4 }];
  return {
    ...knowledgeDetail(),
    chunks: { items: chunks, total: 2, hasMore: !contentRequest && chunkOffset === 0 },
    artifact: { available: true, format: 'markdown', mimeType: 'text/markdown', byteSize: 120, lineCount: 4, sha256: 'c'.repeat(64) },
    contentWindow: contentRequest
      ? { items: lineOffset === 0 ? [{ lineNumber: 1, content: '# 第一段' }, { lineNumber: 2, content: '正文一' }] : [{ lineNumber: 3, content: '# 第二段' }, { lineNumber: 4, content: '正文二' }], total: 4, hasMore: lineOffset === 0 }
      : { items: [{ lineNumber: 1, content: '# 第一段' }], total: 4, hasMore: true },
  };
}

function knowledgeDetail() {
  return {
    document: knowledgeDocument(),
    chunks: { items: [{ chunkId: 'chunk-tool', ordinal: 0, content: 'Agent 启动时注册 knowledge。', page: 12, heading: 'Agent Loop > Tools', tokenCount: 12 }], total: 1, hasMore: false },
    pages: [{ page: 12, chunkCount: 1 }],
    artifact: { available: true, format: 'markdown', mimeType: 'text/markdown', byteSize: 96, lineCount: 3, sha256: 'c'.repeat(64) },
    contentWindow: { items: [{ lineNumber: 1, content: '# Agent Loop' }, { lineNumber: 2, content: 'Agent 启动时注册 knowledge。' }, { lineNumber: 3, content: '![远程图](https://example.invalid/remote.png)' }], total: 3, hasMore: false },
    assets: [{ assetId: 'd'.repeat(64), name: 'tool-flow.png', mimeType: 'image/png', byteSize: 1_024, sha256: 'd'.repeat(64), readPath: `/api/knowledge-bases/kb-runtime/documents/file-runtime/assets/${'d'.repeat(64)}`, page: 12, caption: '工具流程图' }],
    tables: [{ tableId: 'table-tools', title: '工具表', page: 12, columns: ['字段', '值'], rows: [['工具', 'knowledge'], ...Array.from({ length: 21 }, (_value, index) => [`field-${index + 1}`, `value-${index + 1}`])] }],
  };
}

function request(transport: MockControlTransport, pathId: string): ControlRequest | undefined {
  return transport.requests.find((call) => call.request.pathId === pathId)?.request;
}
