import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { PawOsDesktopProvider, type PawOsWindowRequest } from '@/features/paw-os/surface-context';
import {
  createRoomProjection,
  parseRoomConversationSnapshot,
  parseRoomEventSnapshot,
} from '@/contracts/room-reducer';
import { parseRoomEvent } from '@/contracts/validators';
import type { UiRoomEvent } from '@/contracts/ui-events';
import type { AgentSubagentRunV1 } from '@/contracts/generated/agent-subagent-run.v1';
import type { AgentBackgroundJobV1 } from '@/contracts/generated/agent-background-job.v1';
import type { RoomSummary } from '@/features/rooms/room-types';
import { useRoomLiveStore } from '@/features/rooms/state/live-store';
import { clearConversationScrollMemory } from '@/features/conversation-ui';
import { MockControlTransport } from '@/test/mock-transport';
import { useAgentLiveStore } from '@/features/agent/state/live-store';
import { PawOsSatelliteHost } from './PawOsSatelliteHost';
import { PawOsApp } from '@/paw-os/PawOsApp';
import { createPawDesktopStore } from '@/paw-os/runtime/desktop-store';
import { previewRoomSnapshot } from '@/app/preview-room-data';
import { createPreviewTransport } from '@/app/preview-control-transport';
import { ThemeProvider } from '@/design/themes';
import { MotionProvider } from '@/design/motion';
import { GlobalFeedbackProvider } from '@/components/feedback';
import { TooltipProvider, ToastProvider } from '@/components/primitives';
import satelliteCss from './paw-os-satellite.css?raw';

const initialTestUrl = window.location.href;
afterEach(() => {
  cleanup();
  localStorage.removeItem('pawos.desktop.v1');
  window.history.replaceState(null, '', initialTestUrl);
  /* Reading position is remembered per conversation across mounts, so one
     test's scroll must not become the next test's starting point. */
  clearConversationScrollMemory();
  delete document.documentElement.dataset.reduceMotion;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  useAgentLiveStore.setState({ projections: {} });
  useRoomLiveStore.setState({ projections: {} });
});

describe('PawOsSatelliteHost', () => {
  it.each(['focus', 'progress'] as const)('hydrates a cold %s panel through the real desktop while its visible Room main is still unhydrated', async panel => {
    const mounted = mountColdRoomPanel(panel, 'unhydrated');
    try {
      const main = document.querySelector('[data-paw-window-id="agent"]')!;
      expect(main.querySelector('.paw-app-boot')).toBeInTheDocument();
      const window = document.querySelector(`[data-paw-window-id="agent:${mounted.panelId}"]`)!;
      fireEvent.pointerDown(window);
      await within(window as HTMLElement).findByRole('button', { name: '关闭窗口' });
      await waitFor(() => expect(window.querySelector('.paw-os-satellite--room-panel')).toBeInTheDocument());
      expect(main.querySelector('.paw-app-boot')).toBeInTheDocument();
      await waitFor(() => expect(mounted.transport.subscriptionCalls.filter(call => call.request.pathId === 'agent.room.events')).toHaveLength(1));
      expect(useRoomLiveStore.getState().projections[mounted.roomId]?.turnOrder.length).toBeGreaterThan(0);
      expect(mounted.transport.requests.some(call => ['agent.room.conversationSnapshot', 'agent.room.snapshot'].includes(call.request.pathId))).toBe(true);
      const nextRoot = `${mounted.roomId}:live-next`;
      const liveEvent = { ...mounted.snapshot.events[0], eventId: `${mounted.roomId}:live-next`,
        sequence: mounted.snapshot.lastSequence + 1, turnId: nextRoot,
        payload: { rootId: nextRoot, messageId: 'cold-live-message', text: '冷恢复后的新公开消息' },
        resumeToken: `${mounted.roomId}:${mounted.snapshot.lastSequence + 1}` };
      act(() => { expect(mounted.transport.emit('agent.room.events', liveEvent)).toBe(1); });
      await waitFor(() => expect(useRoomLiveStore.getState().projections[mounted.roomId]?.turnOrder).toContain(nextRoot));
      if (panel === 'focus') await within(window as HTMLElement).findByText(/^发出需求「冷恢复后的新公开消息」$/);
      else await waitFor(() => expect(window.querySelector('.agent-status-turn')).toHaveTextContent('0 位伙伴'));
      expect(main.querySelector('.paw-app-boot')).toBeInTheDocument();
      fireEvent.click(within(window as HTMLElement).getByRole('button', { name: '关闭窗口' }));
      await waitFor(() => expect(document.querySelector(`[data-paw-window-id="agent:${mounted.panelId}"]`)).toBeNull());
      await waitFor(() => expect(mounted.transport.activeSubscriptionCount()).toBe(0));
      expect(mounted.requests.some(call => ['agent.room.message', 'agent.room.abort', 'agent.jev.command'].includes(call.pathId))).toBe(false);
    } finally { mounted.unmount(); }
    expect(mounted.transport.activeSubscriptionCount()).toBe(0);
  });


  it.each([
    ['focus', 'closed'], ['focus', 'minimized'],
    ['progress', 'closed'], ['progress', 'minimized'],
  ] as const)('keeps one shared Room subscription when the %s panel and existing keeper cover a %s main', async (panel, main) => {
    const mounted = mountColdRoomPanel(panel, main);
    try {
      await waitFor(() => expect(mounted.transport.subscriptionCalls.filter(call => call.request.pathId === 'agent.room.events')).toHaveLength(1));
      const window = document.querySelector(`[data-paw-window-id="agent:${mounted.panelId}"]`)!;
      fireEvent.pointerDown(window);
      await waitFor(() => expect(window.querySelector('.paw-os-satellite--room-panel')).toBeInTheDocument());
      expect(useRoomLiveStore.getState().projections[mounted.roomId]?.turnOrder.length).toBeGreaterThan(0);
      expect(mounted.transport.subscriptionCalls.filter(call => call.request.pathId === 'agent.room.events')).toHaveLength(1);
      fireEvent.click(within(window as HTMLElement).getByRole('button', { name: '关闭窗口' }));
      await waitFor(() => expect(mounted.transport.activeSubscriptionCount()).toBe(0));
    } finally { mounted.unmount(); }
    expect(mounted.transport.activeSubscriptionCount()).toBe(0);
  });

  it('keeps a governance-only cold panel as detail without acquiring a Room event lease', async () => {
    const mounted = mountColdRoomPanel('governance', 'unhydrated');
    try {
      const window = document.querySelector(`[data-paw-window-id="agent:${mounted.panelId}"]`)!;
      fireEvent.pointerDown(window);
      await waitFor(() => expect(window.querySelector('.paw-room-governance')).toBeInTheDocument());
      expect(mounted.transport.subscriptionCalls.filter(call => call.request.pathId === 'agent.room.events')).toHaveLength(0);
      expect(mounted.transport.requests.some(call => ['agent.room.conversationSnapshot', 'agent.room.snapshot'].includes(call.request.pathId))).toBe(false);
    } finally { mounted.unmount(); }
    expect(mounted.transport.activeSubscriptionCount()).toBe(0);
  });

  it('does not recreate a second Room conversation for a panel-less Room target', () => {
    const transport = new MockControlTransport();

    renderSatellite(transport, {
      kind: 'room', id: 'room-main', title: '产品协作室',
    });

    expect(document.querySelector('.paw-os-satellite')).not.toBeInTheDocument();
    expect(transport.requests).toEqual([]);
  });

  it('renders the real Pi child conversation blocks and event-driven tool state', async () => {
    const run = sampleRun();
    const transport = new MockControlTransport({ routes: {
      'agent.subagents.list': { tree: { roots: [{ run, children: [] }] } },
      'agent.subagent.console': {
        conversation: {
          items: [{
            id: 'message:1',
            role: 'assistant',
            status: 'streaming',
            createdAtMs: 100,
            blocks: [{ type: 'text', data: { text: '正在核对兼容边界。' } }],
          }],
        },
        activity: [{
          id: 'event:1',
          eventType: 'tool_started',
          createdAtMs: 101,
          payload: { toolName: 'read' },
        }],
        inbox: [],
      },
    } });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <ControlTransportProvider transport={transport}>
        <QueryClientProvider client={queryClient}>
          <PawOsSatelliteHost target={{ kind: 'subagent', id: run.id, sessionId: 'session-parent', title: run.task }} />
        </QueryClientProvider>
      </ControlTransportProvider>,
    );

    expect(await screen.findByText('正在核对兼容边界。')).toBeInTheDocument();
    expect(screen.getByText('开始调用 read')).toBeInTheDocument();
    const timeline = screen.getByRole('log', { name: '子 Agent 实现子 Agent 卫星窗 公开对话与运行事件' });
    expect(timeline).toHaveAttribute('aria-live', 'polite');
    expect(screen.getAllByRole('img', { name: '执行中' })).toHaveLength(3);
    expect(document.querySelector('.agent-persona-avatar')).not.toBeInTheDocument();
    expect(document.querySelector('.paw-os-satellite__hero')).not.toBeInTheDocument();
    const statusline = screen.getByLabelText('当前工作与状态');
    expect(statusline).toHaveAttribute('data-state', 'running');
    expect(statusline).toHaveTextContent('进行中');
    expect(statusline).toHaveTextContent('实现子 Agent 卫星窗');
    expect(screen.getByRole('button', { name: '在 Agent 中打开所属 Session' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '查看子 Agent Trace' })).toBeInTheDocument();
  });

  it('fences stale active satellite state with a terminal console run and production lifecycle events', async () => {
    const listRun = sampleRun();
    const completedRun = {
      ...listRun,
      state: 'completed' as const,
      result: { summary: '子 Agent 已交付' },
      updatedAtMs: 120,
      completedAtMs: 120,
    };
    const transport = new MockControlTransport({ routes: {
      'agent.subagents.list': { tree: { roots: [{ run: listRun, children: [] }] } },
      'agent.subagent.console': {
        run: completedRun,
        conversation: { items: [] },
        activity: [
          {
            id: 'event:bookkeeping',
            eventType: 'heartbeat',
            createdAtMs: 99,
            payload: {},
          },
          {
            id: 'event:reasoning',
            eventType: 'reasoning_summary',
            createdAtMs: 100,
            payload: { source: 'provider_reasoning_summary', state: 'running', summary: '正在思考旧摘要' },
          },
          {
            id: 'event:tool-finished',
            eventType: 'tool_finished',
            createdAtMs: 101,
            payload: { toolName: 'read', result: { summary: '读取完成' } },
          },
          {
            id: 'event:turn-completed',
            eventType: 'turn_completed',
            createdAtMs: 102,
            payload: {},
          },
        ],
        inbox: [],
      },
    } });

    renderSatellite(transport, {
      kind: 'subagent', id: listRun.id, sessionId: 'session-parent', title: listRun.task,
    });

    const statusline = await screen.findByLabelText('当前工作与状态');
    expect(statusline).toHaveAttribute('data-state', 'completed');
    expect(statusline).toHaveTextContent('已完成');
    expect(screen.queryByRole('img', { name: '正在思考' })).not.toBeInTheDocument();
    expect(screen.queryByRole('img', { name: '执行中' })).not.toBeInTheDocument();

    const timeline = screen.getByRole('log', { name: '子 Agent 实现子 Agent 卫星窗 公开对话与运行事件' });
    await userEvent.setup().click(screen.getByRole('button', { name: /运行记录/ }));
    expect(timeline.querySelector("article[data-event-type='heartbeat']")).toHaveAttribute('data-status', 'unknown');
    expect(timeline.querySelector("article[data-event-type='reasoning_summary']")).toHaveAttribute('data-status', 'completed');
    expect(timeline.querySelector("article[data-event-type='tool_finished']")).toHaveAttribute('data-status', 'completed');
    expect(timeline.querySelector("article[data-event-type='turn_completed']")).toHaveAttribute('data-status', 'completed');
  });

  it('keeps the terminal console projection after a completed list refresh and remount', async () => {
    const activeRun = sampleRun();
    const completedRun = {
      ...activeRun,
      state: 'completed' as const,
      result: { summary: '持久化交付结果' },
      updatedAtMs: 220,
      completedAtMs: 220,
    };
    const activity = [{
      id: 'event:reasoning',
      eventType: 'reasoning_summary',
      createdAtMs: 100,
      payload: { source: 'provider_reasoning_summary', state: 'running', summary: '历史思考摘要' },
    }, {
      id: 'event:turn-completed',
      eventType: 'turn_completed',
      createdAtMs: 101,
      payload: {},
    }];
    const listSnapshots = [activeRun, completedRun];
    let listRequestCount = 0;
    const transport = new MockControlTransport({ routes: {
      'agent.subagents.list': () => ({
        tree: { roots: [{ run: listSnapshots[Math.min(listRequestCount++, listSnapshots.length - 1)], children: [] }] },
      }),
      'agent.subagent.console': {
        run: completedRun,
        conversation: { items: [] },
        activity,
        inbox: [],
      },
    } });
    const target = {
      kind: 'subagent' as const, id: activeRun.id, sessionId: 'session-parent', title: activeRun.task,
    };

    const first = renderSatellite(transport, target);
    expect(await screen.findByLabelText('当前工作与状态')).toHaveAttribute('data-state', 'completed');
    first.unmount();
    renderSatellite(transport, target);
    await userEvent.setup().click(await screen.findByRole('button', { name: /运行记录/ }));
    expect(await screen.findByText('历史思考摘要')).toBeInTheDocument();

    const statusline = await screen.findByLabelText('当前工作与状态');
    expect(statusline).toHaveAttribute('data-state', 'completed');
    expect(statusline).toHaveTextContent('已完成');
    expect(screen.queryByRole('img', { name: '正在思考' })).not.toBeInTheDocument();
    expect(screen.queryByRole('img', { name: '执行中' })).not.toBeInTheDocument();
  });

  it('surfaces the failed run reason and traces the exact subagent run', async () => {
    const run = {
      ...sampleRun(),
      state: 'failed' as const,
      error: 'workspace read failed: permission denied while opening config.toml',
    };
    const openRoute = vi.fn();
    const transport = new MockControlTransport({ routes: {
      'agent.subagents.list': { tree: { roots: [{ run, children: [] }] } },
      'agent.subagent.console': {
        conversation: { items: [] },
        activity: [{
          id: 'event:failed',
          eventType: 'tool_failed',
          createdAtMs: 101,
          summary: '读取 config.toml 失败',
          payload: { toolName: 'read', status: 'failed', error: 'permission denied from the read tool' },
        }],
        inbox: [],
      },
    } });

    renderSatellite(transport, {
      kind: 'subagent', id: run.id, sessionId: 'session-parent', title: run.task,
    }, { openRoute });

    expect(await screen.findByText(/读取 config\.toml 失败/)).toBeInTheDocument();
    expect(screen.getByText(/permission denied from the read tool/)).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('workspace read failed: permission denied while opening config.toml');
    await userEvent.setup().click(screen.getByRole('button', { name: '查看完整失败原因' }));
    expect(await screen.findByText(run.error)).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole('button', { name: '查看子 Agent Trace' }));
    expect(openRoute).toHaveBeenCalledWith('/observability?runId=subagent-run%3Atest');
  });

  it('reads one authoritative background run log without starting a second command', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.session.backgroundJob.logs': {
        schemaVersion: 'rag-ime.agent-background-job-log.v1', ok: true,
        sessionId: 'session-1', jobId: 'bg_0123456789abcdef0123456789abcdef',
        cursor: 0, nextCursor: 13, logStartCursor: 0, truncatedBeforeCursor: false,
        hasMore: false, text: 'server ready\n',
      },
    } });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <ControlTransportProvider transport={transport}>
        <QueryClientProvider client={queryClient}>
          <PawOsSatelliteHost target={{
            kind: 'process-terminal', id: 'call-job-1', title: 'pnpm dev',
            sessionId: 'session-1', toolCallId: 'call-job-1',
            runId: 'bg_0123456789abcdef0123456789abcdef', command: 'pnpm dev', cwd: '/workspace/paw',
          }} />
        </QueryClientProvider>
      </ControlTransportProvider>,
    );

    expect(await screen.findByText('server ready')).toBeInTheDocument();
    expect(screen.getByText('$ pnpm dev')).toBeInTheDocument();
    expect(transport.requests.map(({ request }) => request.pathId)).toEqual(['agent.session.backgroundJob.logs']);
  });

  it('stops the exact cancellable job and applies its authoritative receipt', async () => {
    const user = userEvent.setup();
    const jobId = 'bg_44444444444444444444444444444444';
    const cancellingJob = backgroundJob('cancelling', jobId);
    const transport = new MockControlTransport({ routes: {
      'agent.session.backgroundJob.logs': {
        schemaVersion: 'rag-ime.agent-background-job-log.v1', ok: true,
        sessionId: 'session-1', jobId, cursor: 0, nextCursor: 0,
        logStartCursor: 0, truncatedBeforeCursor: false, hasMore: false, text: '',
      },
      'agent.session.backgroundJob.cancel': {
        schemaVersion: 'rag-ime.agent-background-job-cancel-receipt.v1',
        ok: true,
        summary: '已请求停止后台任务',
        alreadyTerminal: false,
        job: cancellingJob,
        cancelReceipt: { jobId, requestedAtMs: 200, status: 'cancelling' },
      },
    } });
    renderSatellite(transport, {
      kind: 'process-terminal', id: 'call-job-4', title: 'pnpm dev',
      sessionId: 'session-1', toolCallId: 'call-job-4', runId: jobId,
      command: 'pnpm dev', cwd: '/workspace/paw', runStatus: 'running', roomBound: false,
    });

    await user.click(await screen.findByRole('button', { name: '停止后台任务' }));
    await user.click(screen.getByRole('button', { name: '确认停止后台任务' }));

    await waitFor(() => expect(useAgentLiveStore.getState().projections['session-1']?.backgroundJobsById[jobId])
      .toMatchObject({ status: 'cancelling' }));
    expect(transport.requests.find(({ request }) => request.pathId === 'agent.session.backgroundJob.cancel')?.request).toMatchObject({
      params: { sessionId: 'session-1', jobId },
      body: { reason: 'control_center_requested' },
    });
    expect(screen.getByText('已请求停止后台任务')).toBeVisible();
    expect(screen.queryByRole('button', { name: /停止后台任务/ })).not.toBeInTheDocument();
  });

  it('uses the exact owning session, job, and Room turn when stopping from Room Focus', async () => {
    const user = userEvent.setup();
    const jobId = 'bg_55555555555555555555555555555555';
    const cancellingJob = backgroundJob('cancelling', jobId, {
      sessionId: 'session-room-worker',
      causalMetadata: { todoId: '', todoRevision: 0, goalId: '', goalRevision: 0, turnId: 'room-root-1', roomBound: true },
    });
    const transport = new MockControlTransport({ routes: {
      'agent.session.backgroundJob.logs': {
        schemaVersion: 'rag-ime.agent-background-job-log.v1', ok: true,
        sessionId: 'session-room-worker', jobId,
        cursor: 0, nextCursor: 0, logStartCursor: 0, truncatedBeforeCursor: false,
        hasMore: false, text: '',
      },
      'agent.session.backgroundJob.cancel': {
        schemaVersion: 'rag-ime.agent-background-job-cancel-receipt.v1', ok: true,
        summary: '已请求停止 Room 后台任务', alreadyTerminal: false,
        job: cancellingJob,
        cancelReceipt: { jobId, requestedAtMs: 200, status: 'cancelling' },
      },
    } });
    renderSatellite(transport, {
      kind: 'process-terminal', id: 'call-job-5', title: 'pnpm build',
      sessionId: 'session-room-worker', roomId: 'room-1', toolCallId: 'call-job-5',
      runId: jobId, command: 'pnpm build', runStatus: 'running', roomBound: true,
      roomTurnId: 'room-root-1',
    });

    await user.click(await screen.findByRole('button', { name: '停止后台任务' }));
    await user.click(screen.getByRole('button', { name: '确认停止后台任务' }));

    await waitFor(() => expect(transport.requests.find(({ request }) => request.pathId === 'agent.session.backgroundJob.cancel')?.request).toMatchObject({
      params: { sessionId: 'session-room-worker', jobId },
      body: { reason: 'control_center_requested', roomTurnId: 'room-root-1' },
    }));
    expect(screen.getByText('已请求停止 Room 后台任务')).toBeVisible();
  });

  it('does not guess Room cancel authority when the exact current Room turn is absent', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.session.backgroundJob.logs': {
        schemaVersion: 'rag-ime.agent-background-job-log.v1', ok: true,
        sessionId: 'session-room-worker', jobId: 'bg_66666666666666666666666666666666',
        cursor: 0, nextCursor: 0, logStartCursor: 0, truncatedBeforeCursor: false,
        hasMore: false, text: '',
      },
    } });
    renderSatellite(transport, {
      kind: 'process-terminal', id: 'call-job-6', title: 'pnpm build',
      sessionId: 'session-room-worker', roomId: 'room-1', toolCallId: 'call-job-6',
      runId: 'bg_66666666666666666666666666666666', command: 'pnpm build',
      runStatus: 'running', roomBound: true,
    });

    expect(await screen.findByText('等待运行输出…')).toBeVisible();
    expect(screen.queryByRole('button', { name: /停止后台任务/ })).not.toBeInTheDocument();
  });

  it('renders participant dialogue and Tool lifecycle without a duplicate WorkItem line', async () => {
    const room = participantRoom();
    const projection = createRoomProjection(room.id);
    projection.activityOrder.push('tool-a');
    projection.activitiesById['tool-a'] = {
      id: 'tool-a',
      turnId: 'root-a',
      participantId: 'participant-a',
      sourceSessionId: 'session-a',
      kind: 'tool',
      status: 'running',
      summary: '正在读取 PawWindowLayer.tsx',
      payload: { sourceEventType: 'tool_started', toolName: 'read' },
      createdAtMs: 101,
      updatedAtMs: 101,
    };
    useRoomLiveStore.setState({ projections: { [room.id]: projection } });
    const transport = new MockControlTransport({ routes: {
      'agent.room.get': { room },
    } });

    renderSatellite(transport, {
      kind: 'participant', id: 'participant-a', roomId: room.id,
      title: '实现伙伴', subtitle: '实现 · session-a',
    });

    const timeline = await screen.findByRole('log', { name: '行星公开对话时间线' });
    expect(document.querySelector('.paw-participant-chat__workline')).not.toBeInTheDocument();
    expect(timeline.querySelector('.ccui-tool-card.status-running')).toHaveTextContent('正在读取 PawWindowLayer.tsx');
    expect(screen.queryByRole('banner', { name: '实现伙伴 当前上下文' })).not.toBeInTheDocument();
    expect(document.querySelector('.paw-os-satellite__hero')).not.toBeInTheDocument();
    expect(transport.requests.map(({ request }) => request.pathId).filter((path) => path !== 'agent.session.models')).toEqual([
      'agent.room.get',
      'agent.room.conversationSnapshot',
      'agent.room.snapshot',
    ]);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.models').map(({ request }) => request.params?.sessionId)).toEqual(['session-a']);
  });

  it('hydrates a cold planet from the message-first Room snapshot and applies live messages immediately', async () => {
    const room = participantRoom();
    const initialEvent = participantRoomEvent(1, 'participant_message', {
      message: participantAgentMessage('message-cold', '冷启动的真实消息', 101),
      messageId: 'message-cold',
    });
    const snapshot = participantRoomConversationSnapshot(room, [initialEvent]);
    expect(() => parseRoomConversationSnapshot(snapshot)).not.toThrow();
    const transport = new MockControlTransport({ routes: {
      'agent.room.get': { room },
      'agent.room.conversationSnapshot': snapshot,
    } });

    renderSatellite(transport, {
      kind: 'participant', id: 'participant-a', roomId: room.id,
      title: '实现伙伴', subtitle: '实现 · session-a',
    });

    const timeline = await screen.findByRole('log', { name: '行星公开对话时间线' });
    expect(await within(timeline).findByText('冷启动的真实消息')).toBeInTheDocument();
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));

    const liveEvent = participantRoomEvent(2, 'participant_message', {
      message: participantAgentMessage('message-live', '实时推送已到达', 102),
      messageId: 'message-live',
    });
    act(() => {
      expect(transport.emit('agent.room.events', roomEventWireValue(liveEvent))).toBe(1);
    });
    expect(await within(timeline).findByText('实时推送已到达')).toBeInTheDocument();
  });

  it('keeps a planet read-only while retaining Trace and full Session navigation', async () => {
    const room = participantRoom();
    const projection = participantProjectionWithActivities(room.id, [
      roomActivity('participant-tool-1', 'participant-a', 101, '正在读取 PawWindowLayer.tsx'),
    ]);
    useRoomLiveStore.setState({ projections: { [room.id]: projection } });
    const transport = new MockControlTransport({ routes: { 'agent.room.get': { room } } });
    const openRoute = vi.fn();

    renderSatellite(transport, {
      kind: 'participant', id: 'participant-a', roomId: room.id,
      title: '实现伙伴', subtitle: '实现 · session-a',
    }, { openRoute });

    const statusline = await screen.findByLabelText('当前工作与状态');
    expect(statusline).toHaveClass('paw-participant-chat__statusline');
    expect(statusline.closest('.paw-os-satellite--participant-chat')).toHaveAttribute('data-presentation', 'planet-observer');
    expect(statusline).toHaveAttribute('data-state', 'running');
    expect(statusline).toHaveTextContent('执行中');
    expect(statusline).toHaveTextContent('正在读取 PawWindowLayer.tsx');
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '批准并继续' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '拒绝' })).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole('button', { name: '查看 Mars 的 Trace' }));
    expect(openRoute).toHaveBeenCalledWith('/observability?sessionId=session-a');
    await userEvent.setup().click(screen.getByRole('button', { name: '在 Agent 中打开 Mars 的完整 Session' }));
    expect(openRoute).toHaveBeenCalledWith('/agent?session=session-a');
  });

  it('gives each tool activity one reader line naming the real tool and its state', async () => {
    const room = participantRoom();
    const projection = participantProjectionWithActivities(room.id, [
      { ...roomActivity('participant-tool-1', 'participant-a', 101, '已创建 interface.js'), status: 'completed' },
    ]);
    useRoomLiveStore.setState({ projections: { [room.id]: projection } });
    const transport = new MockControlTransport({ routes: { 'agent.room.get': { room } } });

    renderSatellite(transport, {
      kind: 'participant', id: 'participant-a', roomId: room.id,
      title: '实现伙伴', subtitle: '实现 · session-a',
    });

    const timeline = await screen.findByRole('log', { name: '行星公开对话时间线' });
    // The Runtime tool id reaches the reader as its label (`read` → 读取文件),
    // and the receipt carries its own state instead of a second status row.
    const card = timeline.querySelector<HTMLElement>('.ccui-tool-card');
    expect(card).not.toBeNull();
    expect(card?.querySelector('.ccui-tool-main strong')).toHaveTextContent('读取文件');
    expect(card?.querySelector('.ccui-tool-main span')).toHaveTextContent('已创建 interface.js');
    expect(card?.querySelector('.ccui-tool-meta')).toHaveTextContent('已完成');
    // One card per real Runtime loop, with the loop's actor and time in its head.
    const turn = timeline.querySelector<HTMLElement>('article.ccui-assistant-turn');
    expect(turn?.querySelector('.ccui-assistant-head strong')).toHaveTextContent('Mars');
    expect(turn?.querySelector('.ccui-assistant-head time')).toHaveTextContent(/\d/);
  });

  it('keeps the failure alarm visible on the tool receipt', async () => {
    const room = participantRoom();
    const projection = createRoomProjection(room.id);
    projection.activityOrder.push('tool-failed');
    projection.activitiesById['tool-failed'] = {
      id: 'tool-failed', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
      kind: 'tool', status: 'failed', summary: '命令执行完成，退出码 1',
      payload: { sourceEventType: 'tool_failed', toolName: 'bash' }, createdAtMs: 130, updatedAtMs: 130,
    };
    useRoomLiveStore.setState({ projections: { [room.id]: projection } });
    const transport = new MockControlTransport({ routes: { 'agent.room.get': { room } } });

    renderSatellite(transport, {
      kind: 'participant', id: 'participant-a', roomId: room.id,
      title: '实现伙伴', subtitle: '实现 · session-a',
    });

    const timeline = await screen.findByRole('log', { name: '行星公开对话时间线' });
    const card = timeline.querySelector<HTMLElement>('.ccui-tool-card.status-error');
    expect(card).not.toBeNull();
    expect(card?.querySelector('.ccui-tool-meta')).toHaveTextContent('失败');
    expect(card).toHaveTextContent('命令执行完成，退出码 1');
  });

  it('turns a machine event name in the statusline into readable completion copy', async () => {
    const room = { ...participantRoom(), workItems: [] };
    const projection = participantProjectionWithActivities(room.id, [
      { ...roomActivity('participant-tool-1', 'participant-a', 101, 'participant_activity'), status: 'completed' },
    ]);
    useRoomLiveStore.setState({ projections: { [room.id]: projection } });
    const transport = new MockControlTransport({ routes: { 'agent.room.get': { room } } });

    renderSatellite(transport, {
      kind: 'participant', id: 'participant-a', roomId: room.id,
      title: '实现伙伴', subtitle: '实现 · session-a',
    });

    const statusline = await screen.findByLabelText('当前工作与状态');
    expect(statusline).toHaveAttribute('data-state', 'completed');
    expect(statusline).toHaveTextContent('本轮执行结束');
    expect(statusline).toHaveTextContent('最近有执行记录');
    expect(statusline).not.toHaveTextContent('participant_activity');
    expect(statusline).not.toHaveTextContent('participantactivity');
  });

  it('projects only supported activities addressed to this participant across all event fields', async () => {
    const room = participantRoom();
    const projection = createRoomProjection(room.id);
    const activities = [
      {
        id: 'route-kind', turnId: 'root-a', participantId: null, sourceSessionId: 'session-root',
        kind: 'route_decision', status: 'completed' as const, summary: '路由已确定',
        payload: { targetParticipantId: 'participant-a' }, createdAtMs: 101,
      },
      {
        id: 'dispatch-source', turnId: 'root-a', participantId: null, sourceSessionId: 'session-root',
        kind: 'status', status: 'completed' as const, summary: '已派发',
        payload: { sourceEventType: 'dispatch', targetParticipantId: 'participant-a' }, createdAtMs: 102,
      },
      {
        id: 'intercom-kind', turnId: 'root-a', participantId: null, sourceSessionId: 'session-root',
        kind: 'status', status: 'completed' as const, summary: '伙伴请求已送达',
        payload: { activityKind: 'intercom', targetParticipantId: 'participant-a' }, createdAtMs: 103,
      },
      {
        id: 'tool-kind', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
        kind: 'tool', status: 'running' as const, summary: '正在读取',
        payload: { toolName: 'read' }, createdAtMs: 104,
      },
      {
        id: 'tool-source', turnId: 'root-a', participantId: null, sourceSessionId: 'session-root',
        kind: 'status', status: 'completed' as const, summary: '读取完成',
        payload: { sourceEventType: 'tool_finished', targetParticipantId: 'participant-a', toolName: 'read' }, createdAtMs: 105,
      },
      {
        id: 'route-other', turnId: 'root-a', participantId: null, sourceSessionId: 'session-root',
        kind: 'route_decision', status: 'completed' as const, summary: '不属于当前伙伴',
        payload: { targetParticipantId: 'participant-other' }, createdAtMs: 106,
      },
      {
        id: 'unknown', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
        kind: 'status', status: 'completed' as const, summary: '未知事件不应出现',
        payload: { activityKind: 'not_a_public_event' }, createdAtMs: 107,
      },
      {
        id: 'target-wins', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
        kind: 'dispatch', status: 'completed' as const, summary: '应归属其他目标',
        payload: { targetParticipantId: 'participant-other' }, createdAtMs: 108,
      },
    ];
    projection.activityOrder.push(...activities.map((activity) => activity.id));
    for (const activity of activities) projection.activitiesById[activity.id] = activity;
    useRoomLiveStore.setState({ projections: { [room.id]: projection } });
    const transport = new MockControlTransport({ routes: { 'agent.room.get': { room } } });

    renderSatellite(transport, {
      kind: 'participant', id: 'participant-a', roomId: room.id,
      title: '实现伙伴', subtitle: '实现 · session-a',
    });

    const timeline = await screen.findByRole('log', { name: '行星公开对话时间线' });
    expect(timeline.querySelectorAll('.ccui-tool-card, .ccui-thinking')).toHaveLength(5);
    expect(timeline).toHaveTextContent('路由已确定');
    expect(timeline).toHaveTextContent('已派发');
    expect(timeline).toHaveTextContent('伙伴请求已送达');
    expect(timeline).toHaveTextContent('正在读取');
    expect(timeline).toHaveTextContent('读取完成');
    expect(timeline).not.toHaveTextContent('不属于当前伙伴');
    expect(timeline).not.toHaveTextContent('未知事件不应出现');
    expect(timeline).not.toHaveTextContent('应归属其他目标');
  });

  it('shows a RoomPanel missing state when a successful response has another Room id', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.room.get': { room: { ...participantRoom(), id: 'room-other' } },
    } });

    renderSatellite(transport, {
      kind: 'room', id: 'room-live', panel: 'focus', title: '产品协作室',
    });

    expect(await screen.findByText('找不到这个 Room')).toBeInTheDocument();
    expect(screen.getByText('这个 Room 已不在当前 Room 清单中，可能已归档或删除。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '回到 Room' })).toBeInTheDocument();
  });

  it('projects the consolidated Sol console into the focus satellite and opens real participant targets', async () => {
    const openWindow = vi.fn();
    const room = {
      ...participantRoom(),
      workItems: [{
        id: 'work-a', roomId: 'room-live', topicId: '', rootTurnId: 'root-a', rootWorkId: 'work-a', parentWorkId: '',
        objective: '实现 Room 任务图交互', expectedOutput: '可复查的任务图交互', acceptanceCriteria: ['保持真实依赖关系'],
        accountableParticipantId: 'participant-a', currentOwnerParticipantId: 'participant-a', offeredToParticipantId: '',
        createdByParticipantId: 'participant-a', clientMessageId: '', state: 'review' as const, depth: 0, revision: 2,
        resultSummary: '等待独立复核', artifactRefs: [], evidenceRefs: ['test:room-graph-ui'], blocker: {}, acceptedTurnId: '',
        createdAtMs: 100, updatedAtMs: 120, completedAtMs: null,
      }],
    };
    const transport = new MockControlTransport({ routes: {
      'agent.room.get': { room },
    } });
    const projection = createRoomProjection(room.id);
    projection.messageOrder.push('message-root');
    projection.messagesById['message-root'] = {
      id: 'message-root', roomId: room.id, turnId: 'root-a', participantId: null,
      sourceSessionId: 'session-root', role: 'user', status: 'completed',
      text: '请实现 Room 任务图交互', projectionKind: 'post',
      mentionedParticipantIds: ['participant-a'], createdAtMs: 110,
    };
    useRoomLiveStore.setState({ projections: { [room.id]: projection } });

    const { container } = renderSatellite(transport, {
      kind: 'room', id: room.id, panel: 'focus', title: room.title,
    }, { openWindow });

    const console = await screen.findByRole('region', { name: 'Room 协作' });
    expect(screen.queryByRole('region', { name: 'Sol 协作态势' })).not.toBeInTheDocument();
    fireEvent.click(within(console).getByRole('tab', { name: '任务与回执' }));
    expect(within(console).getByRole('group', { name: '任务与关系详情' })).toHaveTextContent('实现 Room 任务图交互');
    expect(within(console).getByText('验收条件 · 1')).toBeInTheDocument();
    expect(within(console).getByRole('region', { name: '焦点详情' })).toHaveTextContent('等待独立复核');
    expect(container.querySelector('.room-cockpit')).not.toBeInTheDocument();
    expect(container.querySelector('.paw-os-satellite__hero')).not.toBeInTheDocument();

    fireEvent.click(within(console).getByRole('tab', { name: '消息往返' }));
    expect(within(console).getByLabelText('往来事件')).toHaveTextContent('实现 Room 任务图交互');
    expect(console.querySelector('[style*="starfield/"]')).toBeNull();
    fireEvent.click(within(console).getByRole('tab', { name: '任务与回执' }));
    fireEvent.click(within(console).getByRole('button', { name: '打开 Mars 伙伴窗口' }));

    expect(openWindow).toHaveBeenCalledWith(expect.objectContaining({
      appId: 'agent',
      target: expect.objectContaining({ kind: 'participant', id: 'participant-a', roomId: 'room-live', title: 'Mars' }),
    }));
  });

  it('follows participant updates only while the reader is near the latest entry', async () => {
    const room = participantRoom();
    const projection = participantProjectionWithActivities(room.id, [
      roomActivity('participant-tool-1', 'participant-a', 101, '正在读取第一份文件'),
    ]);
    useRoomLiveStore.setState({ projections: { [room.id]: projection } });
    const transport = new MockControlTransport({ routes: { 'agent.room.get': { room } } });

    renderSatellite(transport, {
      kind: 'participant', id: 'participant-a', roomId: room.id,
      title: '实现伙伴', subtitle: '实现 · session-a',
    });

    const timeline = await screen.findByRole('log', { name: '行星公开对话时间线' });
    setScrollMetrics(timeline, 600);
    fireEvent.scroll(timeline);
    const nearBottomProjection = participantProjectionWithActivities(room.id, [
      roomActivity('participant-tool-1', 'participant-a', 101, '正在读取第一份文件'),
      roomActivity('participant-tool-2', 'participant-a', 102, '正在读取第二份文件'),
    ]);
    act(() => useRoomLiveStore.setState({ projections: { [room.id]: nearBottomProjection } }));
    await waitFor(() => expect(timeline).toHaveTextContent('正在读取第二份文件'));
    expect(timeline.scrollTop).toBe(timeline.scrollHeight);

    timeline.scrollTop = 100;
    fireEvent.scroll(timeline);
    const historyProjection = participantProjectionWithActivities(room.id, [
      roomActivity('participant-tool-1', 'participant-a', 101, '正在读取第一份文件'),
      roomActivity('participant-tool-2', 'participant-a', 102, '正在读取第二份文件'),
      roomActivity('participant-tool-3', 'participant-a', 103, '正在读取第三份文件'),
    ]);
    act(() => useRoomLiveStore.setState({ projections: { [room.id]: historyProjection } }));
    await waitFor(() => expect(timeline).toHaveTextContent('正在读取第三份文件'));
    expect(timeline.scrollTop).toBe(100);
  });

  it('preserves subagent history position when a new console entry arrives', async () => {
    const run = sampleRun();
    const consoleSnapshot = {
      conversation: { items: [{ id: 'message:1', role: 'assistant', status: 'completed', createdAtMs: 100, blocks: [{ type: 'text', data: { text: '第一条公开消息。' } }] }] },
      activity: [],
      inbox: [],
    };
    const transport = new MockControlTransport({ routes: {
      'agent.subagents.list': { tree: { roots: [{ run, children: [] }] } },
      'agent.subagent.console': consoleSnapshot,
    } });
    const { queryClient } = renderSatellite(transport, {
      kind: 'subagent', id: run.id, sessionId: 'session-parent', title: run.task,
    });

    const timeline = await screen.findByRole('log', { name: '子 Agent 实现子 Agent 卫星窗 公开对话与运行事件' });
    setScrollMetrics(timeline, 100);
    fireEvent.scroll(timeline);
    await act(async () => {
      queryClient.setQueryData(['agent', 'subagent-satellite', 'session-parent', run.id], {
        ...consoleSnapshot,
        conversation: {
          items: [
            ...consoleSnapshot.conversation.items,
            { id: 'message:2', role: 'assistant', status: 'completed', createdAtMs: 101, blocks: [{ type: 'text', data: { text: '第二条公开消息。' } }] },
          ],
        },
      });
    });

    await waitFor(() => expect(timeline).toHaveTextContent('第二条公开消息。'));
    expect(timeline.scrollTop).toBe(100);
  });

  it('keeps raw public JSON, paths, and hashes behind an explicit participant disclosure', async () => {
    const room = participantRoom();
    const rawDetail = '```json\n{"path":"/Volumes/private/workspace/PawWindowLayer.tsx","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}\n```';
    const projection = createRoomProjection(room.id);
    projection.activityOrder.push('tool-raw');
    projection.activitiesById['tool-raw'] = {
      id: 'tool-raw', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
      kind: 'tool', status: 'running', summary: rawDetail,
      payload: { sourceEventType: 'tool_started', toolName: 'read' },
      createdAtMs: 120, updatedAtMs: 120,
    };
    useRoomLiveStore.setState({ projections: { [room.id]: projection } });
    const transport = new MockControlTransport({ routes: { 'agent.room.get': { room } } });

    renderSatellite(transport, {
      kind: 'participant', id: 'participant-a', roomId: room.id,
      title: '实现伙伴', subtitle: '实现 · session-a',
    });

    const timeline = await screen.findByRole('log', { name: '行星公开对话时间线' });
    // Raw Runtime tool ids map to reader-facing labels (`read` → 读取文件),
    // and the raw call stays reachable instead of becoming the headline.
    const card = timeline.querySelector<HTMLElement>('.ccui-tool-card.status-running');
    expect(card?.querySelector('.ccui-tool-main strong')).toHaveTextContent('读取文件');
    expect(card?.querySelector('.ccui-tool-meta')).toHaveTextContent('正在执行');
    expect(screen.queryByText(/\/Volumes\/private\/workspace\/PawWindowLayer\.tsx/)).not.toBeInTheDocument();
    fireEvent.click(within(card!).getByRole('button'));
    expect(await within(card!).findByText(/PawWindowLayer\.tsx/)).toBeInTheDocument();
    expect(card?.querySelector('pre')).toHaveTextContent('a'.repeat(64));
    fireEvent.click(within(card!).getAllByRole('button')[0]!);
    await waitFor(() => expect(card?.querySelector('pre')).not.toBeInTheDocument());
  });

  it('keeps the whole partner history readable without a windowed history boundary', async () => {
    const room = participantRoom();
    const projection = participantProjectionWithActivities(room.id, Array.from({ length: 49 }, (_, index) => (
      roomActivity(`participant-tool-${index}`, 'participant-a', index + 1, `活动 ${index}`)
    )));
    useRoomLiveStore.setState({ projections: { [room.id]: projection } });
    const transport = new MockControlTransport({ routes: { 'agent.room.get': { room } } });

    renderSatellite(transport, {
      kind: 'participant', id: 'participant-a', roomId: room.id,
      title: '实现伙伴', subtitle: '实现 · session-a',
    });

    const timeline = await screen.findByRole('log', { name: '行星公开对话时间线' });
    // All records remain available through a single group, rather than 49
    // repeated rows obscuring the current task. History is an explicit scope.
    await userEvent.setup().click(screen.getByRole('button', { name: /^全部历史$/ }));
    const currentTimeline = screen.getByRole('log', { name: '行星公开对话时间线' });
    await userEvent.setup().click(within(currentTimeline).getByRole('button', { name: /工具记录 · 49 项/ }));
    const records = screen.getByRole('dialog');
    expect(within(records).getByText(/活动 0$/)).toBeInTheDocument();
    expect(within(records).getByText(/活动 48$/)).toBeInTheDocument();
    expect(records.querySelectorAll('.ccui-tool-card')).toHaveLength(49);
    expect(screen.queryByRole('button', { name: /加载更早的/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/最近 \d+ \/ 共 \d+ 条/)).not.toBeInTheDocument();
  });

  it('keeps a manually collapsed tool receipt collapsed after Runtime completes it', async () => {
    const room = participantRoom();
    const rawSummary = '```json\n{"path":"/workspace/PawWindowLayer.tsx"}\n```';
    const running = participantProjectionWithActivities(room.id, [roomActivity('participant-tool-1', 'participant-a', 101, rawSummary)]);
    useRoomLiveStore.setState({ projections: { [room.id]: running } });
    const transport = new MockControlTransport({ routes: { 'agent.room.get': { room } } });
    renderSatellite(transport, {
      kind: 'participant', id: 'participant-a', roomId: room.id,
      title: '实现伙伴', subtitle: '实现 · session-a',
    });

    const trigger = await screen.findByRole('button', { name: /读取文件/ });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    await userEvent.setup().click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    const completed = participantProjectionWithActivities(room.id, [{ ...roomActivity('participant-tool-1', 'participant-a', 101, rawSummary), status: 'completed' }]);
    act(() => useRoomLiveStore.setState({ projections: { [room.id]: completed } }));
    await waitFor(() => expect(screen.getByRole('button', { name: /读取文件/ })).toHaveAttribute('aria-expanded', 'true'));
  });

  it('makes the 50-run subagent directory bound explicit without inventing a total', async () => {
    const run = sampleRun();
    const runs = Array.from({ length: 50 }, (_, index) => ({ ...run, id: index === 49 ? run.id : `subagent-run:${index}` }));
    const transport = new MockControlTransport({ routes: {
      'agent.subagents.list': { tree: { roots: runs.map((candidate) => ({ run: candidate, children: [] })) } },
      'agent.subagent.console': { conversation: { items: [] }, activity: [], inbox: [] },
    } });
    renderSatellite(transport, { kind: 'subagent', id: run.id, sessionId: 'session-parent', title: run.task });

    expect(await screen.findByText('子 Agent 目录当前加载 50 条；接口未提供总数，较早运行可能未加载（每次最多 50 条）。')).toBeInTheDocument();
    expect(transport.requests.find(({ request }) => request.pathId === 'agent.subagents.list')?.request.query).toMatchObject({ limit: 50 });
    expect(screen.getByRole('button', { name: '在 Agent 中查看' })).toBeInTheDocument();
  });

  it('unmounts a closed tool disclosure instead of leaving its trace in the tree', async () => {
    const user = userEvent.setup();
    const room = participantRoom();
    const projection = participantProjectionWithActivities(room.id, [roomActivity('participant-tool-1', 'participant-a', 101, '正文很长\n需要在公开原文中逐层读取')]);
    useRoomLiveStore.setState({ projections: { [room.id]: projection } });
    const transport = new MockControlTransport({ routes: { 'agent.room.get': { room } } });
    renderSatellite(transport, { kind: 'participant', id: 'participant-a', roomId: room.id, title: '实现伙伴', subtitle: '实现 · session-a' });

    const timeline = await screen.findByRole('log', { name: '行星公开对话时间线' });
    await user.click(await screen.findByRole('button', { name: /读取文件/ }));
    expect(await within(timeline).findByText(/需要在公开原文中逐层读取/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /读取文件/ }));
    expect(within(timeline).queryByText(/需要在公开原文中逐层读取/)).not.toBeInTheDocument();
  });

  it('keeps history actions and disclosure controls reachable in a narrow satellite', () => {
    expect(satelliteCss).toContain('@container paw-window (max-width: 320px)');
    expect(satelliteCss).toContain('.paw-participant-chat__history-boundary { gap: 6px; }');
    expect(satelliteCss).toContain('.paw-participant-chat__activity-group__summary { grid-template-columns: minmax(0, 1fr) 16px 12px; padding-inline: 8px; }');
  });

  it('keeps long English and Chinese satellite labels on one compact row from 320 to 420px', () => {
    // The activity row is a fixed four-column seam: only its message column
    // may shrink. This prevents CJK/long-token wrapping while retaining the
    // state icon, tool glyph, timestamp, and raw-content disclosure.
    expect(satelliteCss).toContain(".paw-participant-chat__timeline article[data-kind='activity'] { display: grid; min-width: 0; grid-template-columns: 16px 16px minmax(0, 1fr) auto;");
    expect(satelliteCss).toContain('.paw-participant-chat__activity-message { min-width: 0; overflow: hidden;');
    expect(satelliteCss).toContain('text-overflow: ellipsis; white-space: nowrap; }');
    expect(satelliteCss).toContain(".paw-participant-chat__timeline article[data-kind='activity'] > .paw-participant-chat__raw-detail { grid-column: 1 / -1;");
    expect(satelliteCss).toContain('.paw-participant-chat__history-boundary > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }');
    expect(satelliteCss).toContain('.paw-participant-chat__activity-group__summary > span { display: flex; min-width: 0; align-items: center; gap: 6px; }');
    expect(satelliteCss).toContain('.paw-participant-chat__activity-group__summary small { min-width: 0; flex: 1 1 auto; overflow: hidden;');
  });

  it('keeps the statusline one text row that never squeezes the dialogue at 280 width', () => {
    expect(satelliteCss).toContain('.paw-os-satellite--participant-chat {\n  display: flex;\n  flex-direction: column;');
    expect(satelliteCss).toContain('.paw-participant-chat__statusline > p { min-width: 0; flex: 1; margin: 0; overflow: hidden; color: var(--paw-ink); text-overflow: ellipsis; white-space: nowrap; }');
    expect(satelliteCss).toContain(".paw-participant-chat__statusline[data-state='running'] { --paw-satellite-state: var(--paw-accent); }");
    expect(satelliteCss).toContain(".paw-participant-chat__statusline[data-state='blocked'],\n.paw-participant-chat__statusline[data-state='failed'] { --paw-satellite-state: var(--paw-danger); }");
    expect(satelliteCss).toContain('.paw-participant-chat__statusline > button {');
    expect(satelliteCss).toContain('white-space: nowrap;');
    expect(satelliteCss).not.toContain('.paw-participant-chat__statusline > button > span { display: none; }');
  });

  it('uses a scoped linear no-card treatment for planet observers', () => {
    expect(satelliteCss).toContain(".paw-os-satellite--participant-chat[data-presentation='planet-observer'] .ccui-user-bubble");
    expect(satelliteCss).toContain(".paw-os-satellite--participant-chat[data-presentation='planet-observer'] .ccui-tool-card");
    expect(satelliteCss).toContain(".paw-os-satellite--participant-chat[data-presentation='planet-observer'] .ccui-error-card");
    expect(satelliteCss).toContain(".paw-os-satellite--participant-chat[data-presentation='planet-observer'] .paw-room-tool-facts");
    expect(satelliteCss).toContain(".paw-os-satellite--participant-chat[data-presentation='planet-observer'] .agent-tool-result-panel");
    expect(satelliteCss).toContain(".paw-os-satellite--participant-chat[data-presentation='planet-observer'] .agent-code-block");
    expect(satelliteCss).toContain('border-left: 1px solid var(--ccui-border);');
    expect(satelliteCss).toContain('border-radius: 0;');
    expect(satelliteCss).toContain('border-block: 1px solid var(--ccui-border);');
    // Narrow planet windows must keep Runtime rows horizontal. Labels and
    // summaries yield with ellipsis; a resize must never turn a one-line
    // activity into the stacked/vertical layout seen in the regression.
    expect(satelliteCss).toContain(".paw-os-satellite--participant-chat[data-presentation='planet-observer'] .ccui-tool-head");
    expect(satelliteCss).toContain('grid-template-columns: 18px minmax(0, 1fr) auto;');
    expect(satelliteCss).toContain(".paw-os-satellite--participant-chat[data-presentation='planet-observer'] .ccui-tool-main strong");
    expect(satelliteCss).toContain(".paw-os-satellite--participant-chat[data-presentation='planet-observer'] .ccui-tool-main strong {\n  min-width: 0;\n  flex: 0 1 auto;");
    expect(satelliteCss).toContain('max-width: 42%;');
    expect(satelliteCss).toContain(".paw-os-satellite--participant-chat[data-presentation='planet-observer'] .ccui-tool-main > span {\n  min-width: 0;\n  flex: 1 1 0;");
    expect(satelliteCss).toContain(".paw-os-satellite--participant-chat[data-presentation='planet-observer'] .ccui-tool-meta");
    expect(satelliteCss).toContain('text-overflow: ellipsis;');
    expect(satelliteCss).toContain(".paw-os-satellite--participant-chat[data-presentation='planet-observer'] .ccui-thinking-summary");
    expect(satelliteCss).toContain(".paw-os-satellite--participant-chat[data-presentation='planet-observer'] .ccui-assistant-head > :is(strong, small)");
    expect(satelliteCss).toContain(".paw-os-satellite--participant-chat[data-presentation='planet-observer'] .ccui-assistant-head > :is(strong, small) {\n  min-width: 0;\n  flex: 0 1 auto;");
    expect(satelliteCss).toContain(".paw-os-satellite--participant-chat[data-presentation='planet-observer'] .ccui-tool-action");
    expect(satelliteCss).toContain('flex-wrap: nowrap;\n  overflow: hidden;\n  white-space: nowrap;');
    expect(satelliteCss).toContain(".ccui-tool-action > :is(button, [role='alert']) {\n  min-width: 0;\n  max-width: 100%;\n  flex: 0 1 auto;");
    expect(satelliteCss).toContain(".paw-os-satellite--participant-chat[data-presentation='planet-observer'] .agent-tool-result-panel__header");
    expect(satelliteCss).toContain('.paw-participant-chat__timeline article > header time { flex: 0 0 auto;');
    // The selector is intentionally rooted at the planet marker; subagent
    // satellite cards and the main Room/Agent surfaces keep their own seam.
    expect(satelliteCss).not.toContain('.ccui-tool-card {\n  border: 0;');
  });
});

function renderSatellite(
  transport: MockControlTransport,
  target: Parameters<typeof PawOsSatelliteHost>[0]['target'],
  desktop?: { openRoute?: (route: string) => void; openWindow?: (request: PawOsWindowRequest) => void },
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const host = (
    <ControlTransportProvider transport={transport}>
      <QueryClientProvider client={queryClient}>
        <PawOsSatelliteHost target={target} />
      </QueryClientProvider>
    </ControlTransportProvider>
  );
  const rendered = render(desktop
    ? <PawOsDesktopProvider openRoute={desktop.openRoute} openWindow={desktop.openWindow ?? (() => undefined)}>{host}</PawOsDesktopProvider>
    : host);
  return { ...rendered, queryClient };
}

function roomActivity(id: string, participantId: string, createdAtMs: number, summary: string) {
  return {
    id,
    turnId: 'root-a',
    participantId,
    sourceSessionId: 'session-a',
    kind: 'tool',
    status: 'running' as 'running' | 'completed',
    summary,
    payload: { sourceEventType: 'tool_started', toolName: 'read' },
    createdAtMs,
  };
}

function participantProjectionWithActivities(roomId: string, activities: ReturnType<typeof roomActivity>[]) {
  const projection = createRoomProjection(roomId);
  projection.activityOrder.push(...activities.map((activity) => activity.id));
  for (const activity of activities) projection.activitiesById[activity.id] = activity;
  return projection;
}

function setScrollMetrics(element: Element, scrollTop: number): asserts element is HTMLElement {
  Object.defineProperties(element, {
    clientHeight: { configurable: true, value: 400 },
    scrollHeight: { configurable: true, value: 1_000 },
    scrollTop: { configurable: true, value: scrollTop, writable: true },
  });
}

function backgroundJob(status: AgentBackgroundJobV1['status'], jobId: string, overrides: Partial<AgentBackgroundJobV1> = {}): AgentBackgroundJobV1 {
  return {
    schemaVersion: 'rag-ime.agent-background-job.v1', jobId, sessionId: 'session-1',
    label: 'pnpm dev', status, command: 'pnpm dev', commandSha256: 'a'.repeat(64),
    cwd: '/workspace/paw', networkAllowed: false, maxRunSeconds: 120, pid: 42,
    createdAtMs: 100, startedAtMs: 110, updatedAtMs: 200, endedAtMs: 0,
    exitCode: null, outputBytes: 0, logStartCursor: 0, logTruncated: false,
    cancelRequestedAtMs: status === 'cancelling' ? 200 : 0, error: '', approvalId: 'approval-1',
    causalMetadata: { todoId: '', todoRevision: 0, goalId: '', goalRevision: 0, turnId: 'turn-1', roomBound: false },
    ...overrides,
  };
}

function sampleRun(): AgentSubagentRunV1 {
  return {
    schemaVersion: 'rag-ime.agent-subagent-run.v1',
    id: 'subagent-run:test',
    nodeId: 'subagent-node:test',
    attemptId: 'subagent-attempt:test:1',
    attemptNumber: 1,
    predecessorAttemptId: '',
    ownerRunId: 'session:parent',
    parentRunId: '',
    depth: 1,
    batchId: 'subagent-batch:test',
    childSessionId: 'session-child',
    todoTask: '核对子 Agent 证据',
    todoPhase: '验证',
    templateId: 'worker',
    templateVersion: '1',
    ordinal: 0,
    task: '实现子 Agent 卫星窗',
    expectedOutput: '真实对话与事件状态',
    acceptanceCriteria: ['真实上下文可见'],
    launchDigest: {
      schemaVersion: 'rag-ime.agent-subagent-launch-digest.v1',
      contextMode: 'fresh',
      templateId: 'worker',
      templateVersion: '1',
      modelProfile: 'openai-codex/gpt-5.6-sol',
      thinkingLevel: 'high',
      toolProfileVersion: 'subagent-v1',
      toolAllowlistMode: 'profile',
      tools: ['read'],
      piSkillsEnabled: false,
      codexSkillsEnabled: false,
      workspaceAccess: 'read_only',
      workspaceRootCount: 1,
      outputContract: { required: false, schemaSha256: '' },
      extensionRuntime: 'pi_host_managed',
    },
    contract: { status: 'not_requested', error: '', toolCallId: '', validatedAtMs: null },
    state: 'running',
    budget: { maxTurns: 0, maxToolCalls: 0, maxTotalTokens: 10_000, maxDurationMs: 60_000, maxOutputChars: 10_000 },
    usage: { turnCount: 1, toolCount: 1, totalTokens: 300 },
    result: {},
    error: '',
    resultContextScheduledAtMs: null,
    createdAtMs: 1,
    startedAtMs: 2,
    updatedAtMs: 3,
    completedAtMs: null,
  };
}

function participantRoom(): RoomSummary {
  return {
    id: 'room-live',
    title: 'PAWOS 完整迁移',
    status: 'active',
    description: '将网页模型结构接入真实 Room reducer。',
    routingPolicy: 'natural',
    moderatorParticipantId: 'participant-root',
    updatedAtMs: 110,
    participants: [{
      id: 'participant-a', sessionId: 'session-a', roleId: 'implementer', roleVersion: '1',
      displayName: '实现伙伴', collaborationRole: 'implementer', status: 'active', ordinal: 1,
    }],
    workItems: [{
      id: 'work-a', roomId: 'room-live', topicId: '', rootTurnId: 'root-a', rootWorkId: 'work-a', parentWorkId: '',
      objective: '完成 Room Focus 生产迁移', expectedOutput: '真实 WorkItem 与 Tool 上下文', acceptanceCriteria: ['不使用静态候选数据'],
      accountableParticipantId: 'participant-a', currentOwnerParticipantId: 'participant-a', offeredToParticipantId: '',
      createdByParticipantId: 'participant-root', clientMessageId: 'client-a', state: 'active', depth: 0, revision: 1,
      resultSummary: '', artifactRefs: [], evidenceRefs: [], blocker: {}, acceptedTurnId: 'turn-a',
      createdAtMs: 100, updatedAtMs: 110, completedAtMs: null,
    }],
  };
}

function participantAgentMessage(id: string, content: string, createdAtMs: number) {
  return {
    schemaVersion: 'rag-ime.agent-message.v1',
    id,
    sessionId: 'session-a',
    turnId: 'root-a',
    role: 'assistant',
    status: 'completed',
    blocks: [{
      id: `${id}:text`,
      type: 'text',
      status: 'completed',
      presentationKind: 'markdown',
      data: { text: content },
    }],
    attachments: [],
    citations: [],
    createdAtMs,
    completedAtMs: createdAtMs,
  };
}

function participantRoomEvent(
  sequence: number,
  eventType: string,
  payload: Record<string, unknown>,
  source: { participantId?: string; sourceSessionId?: string } = {},
): UiRoomEvent {
  return parseRoomEvent({
    schemaVersion: 'rag-ime.agent-room-event.v1',
    eventId: `room-live:${sequence}`,
    roomId: 'room-live',
    sequence,
    turnId: 'root-a',
    eventType,
    participantId: source.participantId ?? 'participant-a',
    sourceSessionId: source.sourceSessionId ?? 'session-a',
    createdAtMs: 100 + sequence,
    payload,
    resumeToken: `room-live:${sequence}`,
  });
}

function roomEventWireValue(event: UiRoomEvent): Record<string, unknown> {
  const { streamKind: _streamKind, ...wireValue } = event;
  return wireValue;
}

function participantRoomSnapshot(room: RoomSummary, events: readonly UiRoomEvent[]) {
  const lastSequence = events.at(-1)?.sequence ?? 0;
  return {
    schemaVersion: 'rag-ime.agent-room-snapshot.v1',
    ok: true,
    room: {
      schemaVersion: 'rag-ime.agent-room.v1',
      id: room.id,
      title: room.title,
      status: room.status,
      description: room.description ?? '',
      routingPolicy: room.routingPolicy,
      moderatorParticipantId: room.moderatorParticipantId ?? 'participant-root',
      workspaceRoots: [],
      executionMode: 'workspace_managed',
      permissionPolicy: {
        schemaVersion: 'rag-ime.room-permission-policy.v1',
        room: { executionMode: 'workspace_managed' },
        partner: { executionMode: 'inherit' },
        toolAgent: { executionMode: 'inherit' },
      },
      createdAtMs: 100,
      updatedAtMs: room.updatedAtMs,
      lastEventSequence: lastSequence,
      participants: [{
        schemaVersion: 'rag-ime.agent-participant.v1',
        id: 'participant-root',
        roomId: room.id,
        sessionId: 'session-root',
        roleId: 'moderator',
        roleVersion: '1',
        displayName: 'Root',
        collaborationRole: 'coordinator',
        status: 'active',
        ordinal: 0,
        createdAtMs: 100,
        lastSpokeAtMs: null,
      }, ...room.participants.map((participant) => ({
        schemaVersion: 'rag-ime.agent-participant.v1',
        roomId: room.id,
        createdAtMs: 100,
        lastSpokeAtMs: null,
        ...participant,
      }))],
      workItems: room.workItems ?? [],
    },
    events: events.map(roomEventWireValue),
    firstSequence: events[0]?.sequence ?? 0,
    lastSequence,
    resumeToken: lastSequence ? `${room.id}:${lastSequence}` : '',
    truncated: false,
  };
}

function participantRoomConversationSnapshot(
  room: RoomSummary,
  events: readonly UiRoomEvent[],
) {
  const full = participantRoomSnapshot(room, events);
  return {
    schemaVersion: 'rag-ime.agent-room-conversation-snapshot.v1',
    ok: true,
    room: full.room,
    events: full.events,
    firstEventSequence: events[0]?.sequence ?? 0,
    cursorSequence: full.lastSequence,
    resumeToken: full.resumeToken,
    deferredEventCount: 0,
    truncated: false,
  };
}


function mountColdRoomPanel(panel: 'focus' | 'progress' | 'governance', main: 'unhydrated' | 'closed' | 'minimized') {
  vi.stubGlobal('matchMedia', (media: string) => ({ media, matches: false, onchange: null, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => true }));
  const roomId = `room-cold-${panel}-${main}`;
  const snapshot = previewRoomSnapshot(roomId);
  const room = snapshot.room as unknown as RoomSummary;
  const transport = new MockControlTransport({ routes: {
    'agent.room.get': { ok: true, room: snapshot.room },
    'agent.room.snapshot': snapshot,
    'agent.room.conversationSnapshot': { schemaVersion: 'rag-ime.agent-room-conversation-snapshot.v1', ok: true,
      room: snapshot.room, events: snapshot.events, firstEventSequence: snapshot.firstSequence,
      cursorSequence: snapshot.lastSequence, resumeToken: snapshot.resumeToken, deferredEventCount: 0, truncated: false },
  } });
  // Only the HTTP/stream boundary is doubled. Settings/directory reads remain
  // offline preview fixtures; the real App/router/lazy window chain is mounted.
  const preview = createPreviewTransport();
  const request = transport.request.bind(transport);
  const requests: Parameters<MockControlTransport['request']>[0][] = [];
  transport.request = call => {
    requests.push(call);
    return call.pathId === 'agent.room.get' || call.pathId === 'agent.room.snapshot' || call.pathId === 'agent.room.conversationSnapshot'
      ? request(call) : preview.request(call);
  };
  const desktop = createPawDesktopStore('agent');
  desktop.getState().bindAgentMain('agent', { kind: 'room', id: roomId, title: 'Cold Room' });
  if (main === 'closed') desktop.getState().closeWindow('agent');
  if (main === 'minimized') desktop.getState().minimizeWindow('agent');
  const panelId = `${roomId}:${panel}`;
  desktop.getState().openApp('agent', { entityId: panelId, target: { kind: 'room', id: roomId, panel, title: 'Cold panel' } });
  desktop.getState().openApp('system-settings', { initialRoute: '/appearance' });
  localStorage.setItem('pawos.desktop.v1', JSON.stringify(desktop.getState()));
  window.history.replaceState(null, '', '#/appearance');
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const rendered = render(<ThemeProvider><MotionProvider><TooltipProvider><ToastProvider><GlobalFeedbackProvider>
    <ControlTransportProvider transport={transport}><QueryClientProvider client={client}><PawOsApp /></QueryClientProvider></ControlTransportProvider>
  </GlobalFeedbackProvider></ToastProvider></TooltipProvider></MotionProvider></ThemeProvider>);
  return { ...rendered, roomId, room, snapshot, transport, requests, panelId };
}
