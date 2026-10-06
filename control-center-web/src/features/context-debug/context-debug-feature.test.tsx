import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { ContextDebugFeature } from '.';

const originalCreateObjectURL = URL.createObjectURL;
const originalRevokeObjectURL = URL.revokeObjectURL;
const createObjectURL = vi.fn(() => 'blob:context-debug');
const revokeObjectURL = vi.fn();

beforeEach(() => {
  createObjectURL.mockClear();
  revokeObjectURL.mockClear();
  Object.defineProperty(URL, 'createObjectURL', {
    configurable: true,
    value: createObjectURL,
  });
  Object.defineProperty(URL, 'revokeObjectURL', {
    configurable: true,
    value: revokeObjectURL,
  });
});

afterEach(() => {
  cleanup();
  restoreUrlMethod('createObjectURL', originalCreateObjectURL);
  restoreUrlMethod('revokeObjectURL', originalRevokeObjectURL);
});

describe('ContextDebugFeature', () => {
  it('shows every model-call delta and truthful parallel tool batch', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          sessions: [{
            id: 'session-a',
            title: 'Debug 会话',
            mode: 'assistant',
            status: 'idle',
            roleId: 'companion-present-v1',
            roleVersion: '1',
            updatedAtMs: 100,
            workspaceRoots: ['/Volumes/work'],
          }],
        },
        'agent.session.debugContext.get': debugContextResponse(),
      },
    });
    renderFeature(transport, '/context-debug?sessionId=session-a');

    expect(await screen.findByText('上下文检查')).toBeInTheDocument();
    const reader = await screen.findByRole('region', { name: '逐次上下文阅读' });
    expect(within(reader).getByRole('navigation', { name: '对话轮次' })).toBeInTheDocument();
    expect(within(reader).getByRole('heading', { name: '最早保留轮次' })).toBeInTheDocument();
    expect(within(reader).getByRole('heading', { name: '第 1 次模型调用' })).toBeInTheDocument();
    expect(within(reader).getByRole('heading', { name: '第 2 次模型调用' })).toBeInTheDocument();
    const callOne = within(reader).getByRole('region', { name: '第 1 次模型调用' });
    expect(within(callOne).getByText('并行 · 2 个工具')).toBeInTheDocument();
    expect(within(callOne).getAllByText(/memory_search/).length).toBeGreaterThan(0);
    expect(within(callOne).getByText('overview')).toBeInTheDocument();
    expect(within(callOne).getByText(/首次上下文/)).toBeInTheDocument();
    expect(within(callOne).getByText(/\+1 \/ -0/)).toBeInTheDocument();
    expect(within(callOne).getByText('检查缓存')).toBeInTheDocument();
    expect(screen.queryByText('回合级旧消息')).not.toBeInTheDocument();

    const tree = within(reader).getByRole('complementary', { name: '上下文目录' });
    expect(within(tree).getByRole('searchbox', { name: '搜索上下文条目' })).toBeInTheDocument();
    await user.click(within(tree).getByRole('button', { name: '用户' }));
    expect(within(tree).queryByRole('button', { name: /memory_search/ })).not.toBeInTheDocument();
    await user.click(within(tree).getByRole('button', { name: '全部' }));
    const userEntry = within(tree).getByRole('button', { name: /用户检查缓存/ });
    await user.click(userEntry);
    expect(userEntry).toHaveAttribute('aria-current', 'location');
    expect(document.activeElement).toHaveAttribute('id', 'context-call-1-message-1');

    await user.click(within(callOne).getByText('请求详情'));
    const requestDetails = within(callOne).getByText('请求详情').closest('details');
    const requestReveal = requestDetails?.querySelector('.context-debug-reader__reveal');
    expect(requestDetails).toHaveAttribute('open');
    expect(requestReveal).toHaveAttribute('aria-hidden', 'false');
    await user.click(within(callOne).getByText('系统指令'));
    expect(screen.getByText('调用一的真实系统提示词')).toBeInTheDocument();
    expect(screen.queryByText('回合级旧系统提示词')).not.toBeInTheDocument();
    await user.click(within(callOne).getByText('工具定义'));
    expect(screen.getByText(/"description": "逐调用真实工具"/)).toBeInTheDocument();
    expect(screen.queryByText(/回合级旧工具/)).not.toBeInTheDocument();
    await user.click(within(callOne).getByText('模型服务交互'));
    expect(screen.getByText(/"model": "gpt-test"/)).toBeInTheDocument();
    await user.click(within(callOne).getByText('请求详情'));
    expect(within(callOne).getByText('请求详情').closest('summary')).toHaveAttribute('aria-expanded', 'false');
    expect(requestDetails).toHaveAttribute('open');
    expect(requestReveal).toBeInTheDocument();
    expect(requestReveal).toHaveAttribute('aria-hidden', 'true');
    expect(screen.getByText('调用一的真实系统提示词')).toBeInTheDocument();
    await waitFor(() => expect(requestDetails).not.toHaveAttribute('open'));
    expect(screen.queryByText('调用一的真实系统提示词')).not.toBeInTheDocument();
    await waitFor(() => expect(transport.requests.some((call) => (
      call.request.pathId === 'agent.session.debugContext.get'
      && call.request.params?.sessionId === 'session-a'
    ))).toBe(true));
  });

  it('keeps the frozen per-call HTML report as a secondary action', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          sessions: [{
            id: 'session-a',
            title: 'Debug 会话',
            mode: 'assistant',
            status: 'idle',
            roleId: 'companion-present-v1',
            roleVersion: '1',
            updatedAtMs: 100,
            workspaceRoots: ['/Volumes/work'],
          }],
        },
        'agent.session.debugContext.get': debugContextResponse(),
      },
    });
    renderFeature(transport, '/context-debug?sessionId=session-a');

    expect(createObjectURL).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    const reportButton = await screen.findByRole('button', { name: '生成 HTML 报告' });
    await user.click(reportButton);

    const dialog = await screen.findByRole('dialog', { name: '逐次上下文装配' });
    expect(dialog).toHaveFocus();
    const frame = within(dialog).getByTitle('逐次上下文报告');
    expect(frame).toHaveAttribute('src', 'blob:context-debug');
    expect(frame).toHaveAttribute('sandbox', 'allow-scripts');
    expect(within(dialog).getByRole('link', { name: '下载报告' })).toHaveAttribute(
      'href',
      'blob:context-debug',
    );
    expect(createObjectURL).toHaveBeenCalledTimes(2);

    await user.click(within(dialog).getByRole('button', { name: '关闭' }));
    await waitFor(() => expect(screen.queryByTitle('逐次上下文报告')).not.toBeInTheDocument());
    expect(reportButton).toHaveFocus();
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:context-debug');
  });

  it('explains that raw context is transient when the runtime has no snapshot', async () => {
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': { ok: true, sessions: [] },
        'agent.session.debugContext.get': {
          available: false,
          transient: true,
          sessionId: 'session-missing',
          turnId: '',
          error: '本机原始上下文调试尚未启用',
          availableTurns: [],
        },
      },
    });
    renderFeature(transport, '/context-debug?sessionId=session-missing');

    expect(await screen.findByText('本机原始上下文调试尚未启用')).toBeInTheDocument();
    expect(screen.getByText('指定会话', { exact: false })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '打开设置' })).toHaveAttribute('href', '#/configuration');
  });

  it.each([
    {
      code: 'session_not_resident',
      expected: '这段对话已经结束或当前未驻留',
    },
    {
      code: 'runtime_unresponsive',
      expected: '运行时没有在诊断等待时间内返回快照',
    },
    {
      code: 'internal_snapshot_lookup_failed',
      expected: '当前回合没有可用的上下文快照',
    },
  ])('explains unavailable historical context without exposing $code', async ({ code, expected }) => {
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          sessions: [{
            id: 'session-old',
            title: '已结束的对话',
            mode: 'assistant',
            status: 'idle',
            roleId: 'companion-present-v1',
            roleVersion: '1',
            updatedAtMs: 100,
            workspaceRoots: [],
          }],
        },
        'agent.session.debugContext.get': {
          available: false,
          transient: true,
          sessionId: 'session-old',
          turnId: '',
          error: code,
          availableTurns: [],
        },
      },
    });
    renderFeature(transport, '/context-debug?sessionId=session-old');

    expect(await screen.findByText(expected, { exact: false })).toBeInTheDocument();
    expect(screen.queryByText(code)).not.toBeInTheDocument();
    expect(screen.queryByText('发送一条消息后', { exact: false })).not.toBeInTheDocument();
  });

  it('keeps long prompt bodies hidden until the user opens the audit payload', async () => {
    const user = userEvent.setup();
    const payload = debugContextResponse();
    const longPrompt = `LONG_PRIVATE_PROMPT_${'x'.repeat(4_000)}`;
    payload.context.prompt = longPrompt;
    Object.assign(payload.context, { contextProjection: {
      stablePrefixMessages: 18,
      dynamicTailMessages: 3,
      sealedMessages: 44,
      pendingMessages: 1,
      compactionState: 'sealed',
      recoveryState: 'ready',
    }, cacheEvidence: [{ requestIndex: 2, prefixSha256: 'abc', prefixBytes: 1024, deltaBytes: 64, duplicateBytes: 1024, inputTokens: 1400, outputTokens: 20, cacheReadTokens: 1200, cacheWriteTokens: 80, capability: 'reported' }] });
    payload.telemetry = { cumulativeUsage: { cacheRead: 1200, cacheWrite: 80 } };
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, sessions: [{ id: 'session-a', title: 'Long prompt', mode: 'assistant', status: 'idle', roleId: 'companion-future-v1', roleVersion: '1', updatedAtMs: 1, workspaceRoots: [] }] },
      'agent.session.debugContext.get': payload,
    } });
    renderFeature(transport, '/context-debug?sessionId=session-a');

    expect(await screen.findByRole('heading', { name: '最早保留轮次' })).toBeVisible();
    expect(screen.getByText('系统与项目指令')).toBeVisible();
    expect(document.body).not.toHaveTextContent(longPrompt);
    await user.click(screen.getByText('本轮原始输入'));
    expect(document.body).toHaveTextContent(longPrompt);
  });

  it('presents memory context as a user-facing reference while keeping the raw payload in details', async () => {
    const user = userEvent.setup();
    const payload = debugContextResponse();
    const rawMemoryContext = '<rag-ime-context type="memory_recall">已召回：用户偏好真实运行时验证。</rag-ime-context>';
    const rawWorkState = '<work-state>当前任务：核对 Provider 上下文顺序。</work-state>';
    const rawLifecycle = '<lifecycle-hook>Session 已启动；压缩后刷新一次上下文。</lifecycle-hook>';
    const rawRecovery = '<compaction-recovery>原始愿景保持不变。已确认 Project/Room 边界与当前实现进度；继续核对 Provider 装配证据。</compaction-recovery>';
    const rawExecutionMode = '<execution-mode mode="full_trust">已授权操作直接执行；硬安全边界继续生效。</execution-mode>';
    const rawWorkflow = '<workflow-state>Todo 正在执行；完成后提交验证回执。</workflow-state>';
    const rawTurnContext = '<turn-context>Agent 正在核对 Project 与 Room 的关联。</turn-context>';
    const rawRoomRecovery = '<room-compaction-recovery>Agent 已恢复 Project 与 Room 的进度。</room-compaction-recovery>';
    payload.context.modelCalls[0].contextDelta.addedMessages.unshift(
      Object.assign({ role: 'custom', content: rawMemoryContext }, { customType: 'rag-ime-memory-recall' }),
      Object.assign({ role: 'custom', content: '已召回：用户偏好成熟产品文案。' }, { customType: 'rag-ime-memory-recall' }),
      Object.assign({ role: 'custom', content: rawWorkState }, { customType: 'rag-ime-work-state' }),
      Object.assign({ role: 'custom', content: rawLifecycle }, { customType: 'rag-ime-lifecycle' }),
      Object.assign({ role: 'custom', content: rawRecovery }, { customType: 'rag-ime-compaction-recovery' }),
      Object.assign({ role: 'custom', content: rawExecutionMode }, { customType: 'rag-ime-execution-mode' }),
      Object.assign({ role: 'custom', content: rawWorkflow }, { customType: 'rag-ime-workflow' }),
      Object.assign({ role: 'custom', content: rawTurnContext }, { customType: 'rag-ime-turn-context' }),
      Object.assign({ role: 'custom', content: rawRoomRecovery }, { customType: 'rag-ime-room-compaction-recovery' }),
      Object.assign({ role: 'custom', content: '对话最近内容' }, { customType: 'rag-ime-session-context' }),
    );
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, sessions: [{ id: 'session-a', title: '记忆参考', mode: 'assistant', status: 'idle', roleId: 'companion-present-v1', roleVersion: '1', updatedAtMs: 1, workspaceRoots: [] }] },
      'agent.session.debugContext.get': payload,
    } });
    const { container } = renderFeature(transport, '/context-debug?sessionId=session-a');

    expect(await screen.findByText('记忆参考：用户偏好真实运行时验证。')).toBeVisible();
    expect(screen.getByText('记忆参考：用户偏好成熟产品文案。')).toBeVisible();
    expect(screen.getByText('当前任务：核对模型服务接收上下文的顺序。')).toBeVisible();
    expect(screen.getByText('对话已开始；完成上下文整理后会刷新本轮内容。')).toBeVisible();
    expect(screen.getByText('原始目标保持不变。项目与协作空间的范围和当前进度已确认；接下来核对模型服务接收的内容。')).toBeVisible();
    expect(screen.getByText('已允许直接执行常规操作；涉及安全边界的操作仍会受保护。')).toBeVisible();
    expect(screen.getByText('当前任务正在进行；完成后会记录验证结果。')).toBeVisible();
    expect(screen.getByText('伙伴正在核对项目与协作空间的关联。')).toBeVisible();
    expect(screen.getByText('伙伴已恢复项目与协作空间的进度。')).toBeVisible();
    expect(screen.getByText('协作空间恢复')).toBeVisible();
    expect(screen.getByText('对话上下文')).toBeVisible();
    const memoryMessages = [...container.querySelectorAll('[data-custom="rag-ime-memory-recall"]')];
    expect(memoryMessages).toHaveLength(2);
    for (const memoryMessage of memoryMessages) {
      expect(memoryMessage.querySelector('.context-debug-session-message__body')).not.toHaveTextContent('<rag-ime-context');
      expect(memoryMessage.querySelector('.context-debug-session-message__body')).not.toHaveTextContent('已召回');
      expect(memoryMessage.querySelector('details')).not.toHaveAttribute('open');
      expect(memoryMessage.querySelector('pre')).not.toBeInTheDocument();
      await user.click(within(memoryMessage as HTMLElement).getByText('原始消息'));
      expect(memoryMessage.querySelector('details')).toHaveAttribute('open');
    }
    for (const customMessage of container.querySelectorAll('[data-custom]:not([data-custom="rag-ime-memory-recall"])')) {
      await user.click(within(customMessage as HTMLElement).getByText('原始消息'));
    }
    expect(memoryMessages[0]?.querySelector('pre')).toHaveTextContent('rag-ime-context');
    expect(container.querySelector('[data-custom="rag-ime-work-state"] pre')).toHaveTextContent('work-state');
    expect(container.querySelector('[data-custom="rag-ime-lifecycle"] pre')).toHaveTextContent('lifecycle-hook');
    expect(container.querySelector('[data-custom="rag-ime-compaction-recovery"] pre')).toHaveTextContent('compaction-recovery');
    expect(container.querySelector('[data-custom="rag-ime-execution-mode"] pre')).toHaveTextContent('execution-mode');
    expect(container.querySelector('[data-custom="rag-ime-workflow"] pre')).toHaveTextContent('workflow-state');
    expect(container.querySelector('[data-custom="rag-ime-turn-context"] pre')).toHaveTextContent('turn-context');
    expect(container.querySelector('[data-custom="rag-ime-room-compaction-recovery"] pre')).toHaveTextContent('room-compaction-recovery');
  });

  it('navigates retained turns and explains initial versus compaction recovery assembly', async () => {
    const user = userEvent.setup();
    const initial = debugContextResponse();
    initial.turnId = 'turn-initial';
    initial.context.turnId = 'turn-initial';
    Object.assign(initial.context, { turnOrdinal: 1, assemblyPhase: 'initial' });
    initial.availableTurns = [
      Object.assign(
        { turnId: 'turn-initial', capturedAtMs: 100, updatedAtMs: 180, modelCallCount: 2, providerRequestCount: 2, toolCallCount: 2, runningToolCount: 0 },
        { turnOrdinal: 1, assemblyPhase: 'initial' as const, summary: '第一次建立上下文' },
      ),
      Object.assign(
        { turnId: 'turn-recovered', capturedAtMs: 200, updatedAtMs: 300, modelCallCount: 2, providerRequestCount: 2, toolCallCount: 2, runningToolCount: 0 },
        { turnOrdinal: 2, assemblyPhase: 'compaction_recovery' as const, summary: '压缩后恢复方向' },
      ),
    ];
    const recovered = structuredClone(initial);
    recovered.turnId = 'turn-recovered';
    recovered.context.turnId = 'turn-recovered';
    Object.assign(recovered.context, { turnOrdinal: 2, assemblyPhase: 'compaction_recovery' });
    recovered.context.modelCalls[0].contextDelta.removedMessageCount = 12;
    recovered.context.modelCalls[0].contextDelta.addedMessages.unshift(Object.assign(
      { role: 'custom', content: '<compaction-recovery>保留原始愿景和当前方向</compaction-recovery>' },
      { customType: 'rag-ime-compaction-recovery' },
    ));
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': { ok: true, sessions: [{ id: 'session-a', title: '多轮上下文', mode: 'assistant', status: 'idle', roleId: 'companion-present-v1', roleVersion: '1', updatedAtMs: 100, workspaceRoots: [] }] },
        'agent.session.debugContext.get': (request: ControlRequest) => (
          request.query?.turnId === 'turn-recovered' ? recovered : initial
        ),
      },
    });
    renderFeature(transport, '/context-debug?sessionId=session-a&turnId=turn-initial');

    expect(await screen.findByRole('heading', { name: '首轮装配' })).toBeInTheDocument();
    await user.type(screen.getByRole('searchbox', { name: '搜索上下文条目' }), 'memory_search');
    const turnNavigation = screen.getByRole('navigation', { name: '对话轮次' });
    await user.click(within(turnNavigation).getByRole('button', { name: /压缩后恢复/ }));
    expect(await screen.findByRole('heading', { name: '压缩后恢复' })).toBeInTheDocument();
    expect(screen.getByRole('searchbox', { name: '搜索上下文条目' })).toHaveValue('memory_search');
    expect(screen.getByText('旧消息已被低分辨率恢复材料替代', { exact: false })).toBeInTheDocument();
    expect(screen.getByText('压缩恢复与运行时')).toBeInTheDocument();
    expect(screen.getByText('+2 / -12')).toBeInTheDocument();
    await waitFor(() => expect(transport.requests.some((call) => (
      call.request.pathId === 'agent.session.debugContext.get'
      && call.request.query?.turnId === 'turn-recovered'
    ))).toBe(true));
  });
});

function renderFeature(transport: MockControlTransport, initialEntry: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <ControlTransportProvider transport={transport}>
        <QueryClientProvider client={client}>
          <TooltipProvider>
            <ContextDebugFeature />
          </TooltipProvider>
        </QueryClientProvider>
      </ControlTransportProvider>
    </MemoryRouter>,
  );
}

function debugContextResponse() {
  return {
    available: true,
    transient: true,
    sessionId: 'session-a',
    turnId: 'turn-a',
    availableTurns: [{ turnId: 'turn-a', capturedAtMs: 100, updatedAtMs: 300, modelCallCount: 2, providerRequestCount: 2, toolCallCount: 2, runningToolCount: 0 }],
    context: {
      sessionId: 'session-a',
      turnId: 'turn-a',
      capturedAtMs: 100,
      updatedAtMs: 300,
      prompt: '原始用户输入',
      systemPrompt: '回合级旧系统提示词',
      systemPromptOptions: { cwd: '/Volumes/work' },
      model: { provider: 'gpt', name: 'GPT Test' },
      activeTools: ['memory_search', 'overview'],
      toolSchemas: [{ name: '回合级旧工具' }],
      modelCalls: [
        {
          index: 1,
          runtimeTurnIndex: 0,
          capturedAtMs: 100,
          updatedAtMs: 180,
          completedAtMs: 180,
          contextMessages: [{ role: 'user', content: '回合级旧消息' }],
          providerContext: {
            systemPrompt: '调用一的真实系统提示词',
            messages: [{ role: 'user', content: '检查缓存' }],
            tools: [{
              name: 'memory_search',
              description: '逐调用真实工具',
            }, { name: 'overview' }],
          },
          contextDelta: { commonPrefixMessages: 0, removedMessageCount: 0, addedMessageCount: 1, addedMessages: [{ role: 'user', content: '检查缓存' }] },
          providerExchanges: [{ index: 1, capturedAtMs: 120, status: 200, headers: { request: 'one' }, payload: { model: 'gpt-test', input: '检查缓存' } }],
          assistantMessage: { role: 'assistant', content: [{ type: 'toolCall', name: 'memory_search' }] },
        },
        {
          index: 2,
          runtimeTurnIndex: 1,
          capturedAtMs: 220,
          updatedAtMs: 300,
          completedAtMs: 300,
          contextMessages: [{ role: 'user', content: '检查缓存' }, { role: 'toolResult', content: '命中' }],
          providerContext: {
            systemPrompt: '调用二的真实系统提示词',
            messages: [{ role: 'user', content: '检查缓存' }, { role: 'toolResult', content: '命中' }],
            tools: [{ name: 'memory_search' }, { name: 'overview' }],
          },
          contextDelta: { baseCallIndex: 1, commonPrefixMessages: 1, removedMessageCount: 0, addedMessageCount: 1, addedMessages: [{ role: 'toolResult', content: '命中' }] },
          providerExchanges: [{ index: 2, capturedAtMs: 240, status: 200, headers: { request: 'two' }, payload: { model: 'gpt-test', input: '命中' } }],
          assistantMessage: { role: 'assistant', content: '完成' },
        },
      ],
      toolExecutions: [
        { toolCallId: 'tool-1', toolName: 'memory_search', modelCallIndex: 1, runtimeTurnIndex: 0, startedAtMs: 130, endedAtMs: 170, startSequence: 1, endSequence: 4, args: { query: '缓存' }, result: { count: 2 }, status: 'completed', updates: [] },
        { toolCallId: 'tool-2', toolName: 'overview', modelCallIndex: 1, runtimeTurnIndex: 0, startedAtMs: 135, endedAtMs: 165, startSequence: 2, endSequence: 3, args: { op: 'status' }, result: { healthy: true }, status: 'completed', updates: [] },
      ],
      toolBatches: [{ id: 'batch-1', modelCallIndex: 1, runtimeTurnIndex: 0, stage: 1, executionMode: 'parallel', startedAtMs: 130, endedAtMs: 170, status: 'completed', toolCallIds: ['tool-1', 'tool-2'] }],
    },
    telemetry: {},
  };
}

function restoreUrlMethod(
  name: 'createObjectURL' | 'revokeObjectURL',
  value: typeof URL.createObjectURL | typeof URL.revokeObjectURL | undefined,
): void {
  if (value) {
    Object.defineProperty(URL, name, {
      configurable: true,
      value,
    });
    return;
  }
  Reflect.deleteProperty(URL, name);
}
