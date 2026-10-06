import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { PawOsAppActivityProvider } from '@/features/paw-os/surface-context';
import type { ControlPathId } from '@/platform/routes';
import type {
  ControlEventObserver,
  ControlRequest,
  ControlSubscription,
  ControlTransport,
  FrontendCapabilities,
} from '@/platform/transport';
import { HistoryFeature } from '.';

const hash = 'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc';

afterEach(cleanup);

describe('History WorkContract UI', () => {
  it('closes a detail portal when its retained page becomes inactive and keeps the unsent search', async () => {
    const user = userEvent.setup();
    const transport = new HistoryTransport();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const page = (active: boolean) => <MemoryRouter><TooltipProvider delayDuration={0}>
      <ControlTransportProvider transport={transport}><QueryClientProvider client={client}>
        <PawOsAppActivityProvider active={active}><HistoryFeature /></PawOsAppActivityProvider>
      </QueryClientProvider></ControlTransportProvider>
    </TooltipProvider></MemoryRouter>;
    const view = render(page(true));
    const input = await screen.findByRole('textbox', { name: '搜索' });
    await user.type(input, '还未提交的搜索');
    await user.click(await screen.findByRole('button', { name: /查看 .* 的输入详情/ }));
    await screen.findByRole('dialog', { name: '输入详情' });
    view.rerender(page(false));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '输入详情' })).not.toBeInTheDocument());
    view.rerender(page(true));
    expect(screen.getByRole('textbox', { name: '搜索' })).toBe(input);
    expect(input).toHaveValue('还未提交的搜索');
    expect(screen.queryByRole('dialog', { name: '输入详情' })).not.toBeInTheDocument();
  });

  it('keeps the search input and focus through a slow search and ignores IME confirmation Enter', async () => {
    const user = userEvent.setup();
    const transport = renderHistory(new HistoryTransport((request) => request.query?.query
      ? new Promise(() => {}) : historyPage()));
    const input = await screen.findByRole('textbox', { name: '搜索' });
    await user.type(input, '项目');
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true });
    expect(transport.requests.filter(({ pathId }) => pathId === 'history.page')).toHaveLength(1);
    await user.keyboard('{Enter}');
    await waitFor(() => expect(transport.requests.some((request) => request.query?.query === '项目')).toBe(true));
    expect(screen.getByRole('textbox', { name: '搜索' })).toBe(input);
    expect(input).toHaveFocus();
    await user.clear(input);
    await user.keyboard('{Enter}');
    expect(await screen.findByText('完成了...')).toBeInTheDocument();
    expect(input).toHaveFocus();
  });

  it('retains loaded history when the next page fails and retries the same cursor', async () => {
    const user = userEvent.setup();
    let recovered = false;
    const transport = renderHistory(new HistoryTransport((request) => {
      if (!request.query?.cursor) return { ...historyPage(), nextCursor: 'page-two' };
      if (!recovered) throw new Error('next page unavailable');
      return { ...historyPage(), items: [{ ...historyPage().items[0], id: 82, textPreview: '第二页记录' }] };
    }));
    await user.click(await screen.findByRole('button', { name: '加载下一页' }));
    expect(await screen.findByText('后续记录未能加载')).toBeInTheDocument();
    expect(screen.getByText('完成了...')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '搜索' })).toBeInTheDocument();
    recovered = true;
    await user.click(screen.getByRole('button', { name: '重试加载更多' }));
    expect(await screen.findByText('第二页记录')).toBeInTheDocument();
    expect(screen.getByText('完成了...')).toBeInTheDocument();
    expect(transport.requests.filter(({ pathId }) => pathId === 'history.page').map((request) => request.query?.cursor)).toEqual(['', 'page-two', 'page-two']);
  });

  it('clears a mutation selection when a search replaces the visible records', async () => {
    const user = userEvent.setup();
    const transport = renderHistory(new HistoryTransport((request) => request.query?.query
      ? { ...historyPage(), items: [{ ...historyPage().items[0], id: 82, textPreview: '另一条记录' }] }
      : historyPage()));
    await user.click(await screen.findByRole('combobox', { name: '选择记录' }));
    await user.click(await screen.findByRole('option', { name: /完成了/ }));
    await user.type(screen.getByRole('textbox', { name: '搜索' }), '另一条{Enter}');
    await screen.findByText('另一条记录');
    expect(screen.queryByRole('button', { name: '不再用于记忆' })).not.toBeInTheDocument();
    expect(screen.getByText('先从已加载记录中选择一项。')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: '选择记录' })).toHaveTextContent('请选择一条记录');
    expect(findRequest(transport, 'history.tombstone.preview')).toBeUndefined();
  });

  it('renders the history filters as one responsive search toolbar', async () => {
    renderHistory();

    const toolbar = await screen.findByRole('search', { name: '筛选输入记录' });
    expect(toolbar).toHaveClass('history-filter-toolbar');
    expect(within(toolbar).getByRole('textbox', { name: '搜索' })).toBeInTheDocument();
    expect(within(toolbar).getByRole('combobox', { name: '来源' })).toBeInTheDocument();
    expect(within(toolbar).getByRole('button', { name: '查找' })).toBeInTheDocument();
  });

  it('opens a real full-text detail from the keyboard and exposes only verified server state', async () => {
    const user = userEvent.setup();
    const transport = renderHistory();
    const openDetail = await screen.findByRole('button', { name: /查看 .* 的输入详情/ });

    openDetail.focus();
    await user.keyboard('{Enter}');

    const dialog = await screen.findByRole('dialog', { name: '输入详情' });
    expect(within(dialog).getByText('这是服务端按事件读取的完整输入，不是列表摘要。')).toBeInTheDocument();
    expect(within(dialog).queryByText('豆包语音')).not.toBeInTheDocument();
    expect(within(dialog).getAllByText('采用')).toHaveLength(2);
    expect(within(dialog).getByText('2 次')).toBeInTheDocument();
    expect(within(dialog).getByText('1 次')).toBeInTheDocument();
    expect(within(dialog).getByText('PAW')).toBeInTheDocument();
    expect(within(dialog).getByRole('heading', { name: '上下文获取' })).toBeInTheDocument();
    expect(within(dialog).getByText('前面正在核对来源筛选，随后完成了当前输入。')).toBeInTheDocument();
    expect(within(dialog).queryByText('语音定稿插入')).not.toBeInTheDocument();
    const captureSummary = within(dialog).getByText('高级：采集详情').closest('summary');
    expect(captureSummary).not.toBeNull();
    await user.click(captureSummary!);
    expect(captureSummary).toHaveAttribute('aria-expanded', 'true');
    expect(within(dialog).getByText('豆包语音')).toBeInTheDocument();
    expect(within(dialog).getByText('语音定稿插入')).toBeInTheDocument();
    expect(within(dialog).getByText('不适用 · 语音定稿不请求智能候选')).toBeInTheDocument();
    expect(within(dialog).getByText('14 字 · 已记录')).toBeInTheDocument();
    expect(within(dialog).getByText('0 字 · 语音输入不经过输入法缓冲区')).toBeInTheDocument();
    expect(within(dialog).getByText('已存入 · 强边界')).toBeInTheDocument();
    expect(within(dialog).queryByText('其他采集方式')).not.toBeInTheDocument();
    expect(within(dialog).queryByText('未关联')).not.toBeInTheDocument();
    expect(within(dialog).queryByText('wisdom-weasel-rag-ime')).not.toBeInTheDocument();
    expect(findRequest(transport, 'history.detail')).toMatchObject({ query: { eventId: 81 } });
    await user.keyboard('{Escape}');
    await waitFor(() => expect(openDetail).toHaveFocus());
    expect(historyPage().items[0]).not.toHaveProperty('text');
  });

  it('binds a selected event to tombstone apply and rollback receipts', async () => {
    const user = userEvent.setup();
    const transport = renderHistory();
    await screen.findByRole('heading', { name: '输入记录', level: 1 });
    await user.click(await screen.findByRole('combobox', { name: '选择记录' }));
    await user.click(await screen.findByRole('option', { name: /完成了/ }));
    const workflow = screen.getByText('不再用于记忆', { selector: 'strong' }).closest('.mgmt-workflow');
    expect(workflow).not.toBeNull();
    expect(workflow).toHaveAttribute('data-confirmation', 'direct');

    await user.click(within(workflow as HTMLElement).getByRole('button', { name: '不再用于记忆' }));
    await waitFor(() => expect(findRequest(transport, 'history.tombstone.preview')).toMatchObject({
      body: {
        eventId: 81,
        reason: 'control-center-history',
        expectedRuntimeRevision: 9,
      },
    }));
    expect(within(workflow as HTMLElement).queryByRole('checkbox')).not.toBeInTheDocument();
    expect(within(workflow as HTMLElement).queryByRole('button', { name: '确认执行' })).not.toBeInTheDocument();
    expect(await within(workflow as HTMLElement).findByText('已保存')).toBeInTheDocument();
    expect(findRequest(transport, 'history.tombstone.apply')).toMatchObject({
      body: {
        eventId: 81,
        previewToken: 'preview-history-hide',
        payloadSha256: hash,
        confirmText: 'apply',
      },
    });
    await user.click(within(workflow as HTMLElement).getByRole('button', { name: '撤销' }));
    expect(await within(workflow as HTMLElement).findByText('已恢复到更改前')).toBeInTheDocument();
    expect(findRequest(transport, 'history.tombstone.rollback')).toMatchObject({
      body: {
        receiptId: 'receipt-history-hide',
        rollbackToken: 'rollback-history-hide',
        payloadSha256: hash,
        confirmText: 'rollback',
      },
    });
  });

  it('does not offer a fake negative-feedback mutation', async () => {
    renderHistory();
    expect(await screen.findByText('选择一条记录后，可以让它以后不再用于联想。原始记录仍会保留，操作也可以撤销。')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '演练流程' })).not.toBeInTheDocument();
  });
});

class HistoryTransport implements ControlTransport {
  readonly kind = 'mock' as const;
  readonly requests: ControlRequest[] = [];

  constructor(private readonly pageResponse: (request: ControlRequest) => unknown = historyPage) {}

  async capabilities(): Promise<FrontendCapabilities> {
    return {
      schemaVersion: 'rag-ime.control-frontend-capabilities.v1',
      transport: 'mock',
      routeIds: [
        'history.page',
        'history.detail',
        'history.tombstone.preview',
        'history.tombstone.apply',
        'history.tombstone.rollback',
      ] as ControlPathId[],
      features: { managementWorkContract: true, historyWorkContract: true },
      native: { pickFiles: false, managedAgentImageImport: false, revealPath: false, approvedExternalActions: false, keychain: false, tcc: false },
    };
  }

  async request<Response = unknown>(request: ControlRequest): Promise<Response> {
    this.requests.push(request);
    if (request.pathId === 'history.page') return await this.pageResponse(request) as Response;
    if (request.pathId === 'history.detail') return historyDetail() as Response;
    if (request.pathId === 'history.tombstone.preview') return {
      schemaVersion: 'rag-ime.management-work-preview.v1',
      ok: true,
      previewToken: 'preview-history-hide',
      pathId: 'history.tombstone.apply',
      payloadSha256: hash,
      expectedRevision: { runtimeRevision: 9, subjectRevision: 'sha256:before' },
      expiresAtMs: Date.now() + 60_000,
      requiredConfirm: 'apply',
      summary: {
        title: '隐藏输入历史记录',
        items: ['记录 ID: 81', '停止参与后续召回。'],
        risk: 'R2',
      },
    } as Response;
    if (request.pathId === 'history.tombstone.apply') return receipt(
      'history.tombstone.apply',
      'receipt-history-hide',
      'rollback-history-hide',
      true,
    ) as Response;
    if (request.pathId === 'history.tombstone.rollback') return receipt(
      'history.tombstone.rollback',
      'receipt-history-rollback',
      '',
      false,
    ) as Response;
    throw new Error(`Unexpected request: ${request.pathId}`);
  }

  subscribe<Event = unknown>(_request: ControlSubscription, _observer: ControlEventObserver<Event>): () => void {
    return () => {};
  }
}

function renderHistory(transport = new HistoryTransport()): HistoryTransport {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <MemoryRouter>
      <TooltipProvider delayDuration={0}>
        <ControlTransportProvider transport={transport}>
          <QueryClientProvider client={client}><HistoryFeature /></QueryClientProvider>
        </ControlTransportProvider>
      </TooltipProvider>
    </MemoryRouter>,
  );
  return transport;
}

function findRequest(transport: HistoryTransport, pathId: string): ControlRequest | undefined {
  return transport.requests.find((request) => String(request.pathId) === pathId);
}

function historyPage() {
  return {
    ok: true,
    runtimeRevision: 9,
    items: [{
      id: 81,
      createdAtMs: 1_784_006_400_000,
      source: 'rime_commit',
      app: 'TextEdit',
      project: 'wisdom-weasel-rag-ime',
      textPreview: '完成了...',
      textChars: 6,
      contextHash: 'sha256:test',
    }],
    nextCursor: '',
    limit: 50,
    rawTextVisible: false,
  };
}

function historyDetail() {
  return {
    ok: true,
    runtimeRevision: 9,
    rawTextVisible: true,
    item: {
      id: 81,
      createdAtMs: 1_784_006_400_000,
      source: 'voice',
      text: '这是服务端按事件读取的完整输入，不是列表摘要。',
      textChars: 23,
      app: 'TextEdit',
      project: 'wisdom-weasel-rag-ime',
      provider: 'volcengine-asr',
      candidateRank: 1,
      groupId: 'document:test',
      groupLevel: 'document',
      auxiliaryContext: {
        available: true,
        text: '前面正在核对来源筛选，随后完成了当前输入。',
        textChars: 22,
        truncated: false,
        hasAdditionalText: true,
        captureSource: 'voice_insertion',
        captureMode: '',
        fallbackReason: '',
        fieldContextRecorded: true,
        fieldContextChars: 14,
        imeBufferRecorded: true,
        imeBufferChars: 0,
        modelRequestAssociation: 'not_applicable',
        modelRequestReason: 'voice_capture_does_not_request_assistant_candidates',
        modelRequestLinked: false,
        captureReceipt: {
          available: true,
          channel: 'voice',
          boundaryKind: 'voice_final',
          boundaryConfidence: 'strong',
          outcome: 'stored',
          reason: 'strong_final_boundary',
          evidenceState: 'candidate',
          evidenceReason: 'awaiting_luna_adjudication',
        },
      },
      status: 'active',
      feedback: {
        available: true,
        acceptedCount: 2,
        skippedCount: 1,
        pinned: false,
        downranked: false,
        deleted: false,
        updatedAtMs: 1_784_006_400_000,
        latestAction: 'accept',
        latestActionAtMs: 1_784_006_400_000,
      },
    },
  };
}

function receipt(pathId: string, receiptId: string, rollbackToken: string, rollbackAvailable: boolean) {
  return {
    schemaVersion: 'rag-ime.management-work-receipt.v1',
    ok: true,
    receiptId,
    pathId,
    payloadSha256: hash,
    appliedAtMs: Date.now(),
    auditId: 1,
    rollbackAvailable,
    rollbackToken,
    rollbackAuthority: { eventId: 81 },
    restartComponents: [],
  };
}
