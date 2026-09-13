import { forwardRef, useImperativeHandle, useLayoutEffect, useRef, type Key, type ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { createPreviewTransport } from '@/app/preview-control-transport';
import { TooltipProvider } from '@/components/primitives';
import { MockControlTransport } from '@/test/mock-transport';
import { createRoomProjection, reduceRoomEvent } from '@/contracts/room-reducer';
import { parseRoomEvent } from '@/contracts/validators';
import type { WorkDocumentV1 } from '@/contracts/work-documents';
import { previewPersonas, previewTemplates } from '@/features/agent/preview-data';
import { FilePreviewHost } from '@/features/agent/file-preview/FilePreviewHost';
import { PawOsDesktopProvider, type PawOsWindowRequest } from '@/features/paw-os/surface-context';
import type { ControlRequest, PickedFile } from '@/platform/transport';
import { RoomTurn, RoomsFeature, type RoomSummary } from './index';
import { RoomStatusPanel } from './RoomStatusPanel';
import { useRoomLiveStore } from './state/live-store';

const roomVirtuosoMock = vi.hoisted(() => ({
  scrollToIndex: vi.fn(),
  scrollTo: vi.fn(),
  atBottomStateChange: undefined as ((atBottom: boolean) => void) | undefined,
  followOutput: undefined as false | ((isAtBottom: boolean) => 'auto' | 'smooth' | false) | undefined,
  scroller: undefined as HTMLDivElement | undefined,
}));

vi.mock('react-virtuoso', () => ({
  Virtuoso: forwardRef(function MockVirtuoso({
    atBottomStateChange,
    components,
    context,
    computeItemKey,
    data,
    followOutput,
    initialTopMostItemIndex,
    itemContent,
    scrollerRef,
    startReached,
  }: {
    atBottomStateChange?: (atBottom: boolean) => void;
    components?: {
      Header?: (props: { context?: unknown }) => ReactNode;
      Footer?: () => ReactNode;
    };
    context?: unknown;
    computeItemKey?: (index: number, item: string) => Key;
    data: string[];
    followOutput?: false | ((isAtBottom: boolean) => 'auto' | 'smooth' | false);
    initialTopMostItemIndex?: { index: string | number; align?: string };
    itemContent: (index: number, item: string) => ReactNode;
    scrollerRef?: (scroller: HTMLElement | Window | null) => void;
    startReached?: (index: number) => void;
  }, ref) {
    const Header = components?.Header;
    const Footer = components?.Footer;
    const localScrollerRef = useRef<HTMLDivElement>(null);
    useImperativeHandle(ref, () => ({
      scrollToIndex: roomVirtuosoMock.scrollToIndex,
    }));
    useLayoutEffect(() => {
      roomVirtuosoMock.scroller = localScrollerRef.current ?? undefined;
      if (localScrollerRef.current) localScrollerRef.current.scrollTo = roomVirtuosoMock.scrollTo;
      scrollerRef?.(localScrollerRef.current);
      return () => {
        scrollerRef?.(null);
        roomVirtuosoMock.scroller = undefined;
      };
    }, [scrollerRef]);
    roomVirtuosoMock.atBottomStateChange = atBottomStateChange;
    roomVirtuosoMock.followOutput = followOutput;
    return (
      <div
        ref={localScrollerRef}
        data-virtuoso-scroller="true"
        data-initial-align={initialTopMostItemIndex?.align}
        data-initial-index={initialTopMostItemIndex?.index}
        data-testid="virtuoso-list"
      >
        {Header ? <Header context={context} /> : null}
        {startReached ? <button onClick={() => startReached(0)} type="button">模拟到达历史顶部</button> : null}
        {data.map((item, index) => <div key={computeItemKey?.(index, item) ?? index}>{itemContent(index, item)}</div>)}
        {Footer ? <Footer /> : null}
      </div>
    );
  }),
}));
describe('Rooms experience', () => {
  afterEach(() => {
    cleanup();
    roomVirtuosoMock.scrollToIndex.mockReset();
    roomVirtuosoMock.scrollTo.mockReset();
    roomVirtuosoMock.atBottomStateChange = undefined;
    roomVirtuosoMock.followOutput = undefined;
    roomVirtuosoMock.scroller = undefined;
    useRoomLiveStore.getState().reset();
    window.localStorage.clear();
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined });
  });

  it('steers an active Room turn immediately and preserves the selected room id', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': {
        ok: true,
        items: [{
          id: 'room-a', title: '迁移作战室', status: 'active', routingPolicy: 'moderator',
          moderatorParticipantId: 'p1', updatedAtMs: Date.now(),
          participants: [
            { id: 'p1', sessionId: 's1', roleId: 'companion-present-v1', roleVersion: '1', displayName: '澄', status: 'active', ordinal: 0 },
            { id: 'p2', sessionId: 's2', roleId: 'companion-firstlight-v1', roleVersion: '1', displayName: '澄·初', status: 'active', ordinal: 1 },
          ],
        }],
      },
      'agent.room.snapshot': roomSnapshot('room-a', [
        roomEvent('room-a', 1, 'user_message', { text: '快照中的历史消息' }),
      ]),
      'agent.roles.list': { ok: true, items: [] },
      'agent.room.message': { ok: true },
      'agent.room.participant.steer': { ok: true },
      'agent.rooms.create': { ok: true },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);
    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    expect(screen.queryByAltText(/两位伙伴在私有工作区之间显式交接/)).not.toBeInTheDocument();
    const errorSlot = document.querySelector('.room-error-slot');
    expect(errorSlot).toBeInTheDocument();
    expect(errorSlot).toBeEmptyDOMElement();
    expect(errorSlot?.nextElementSibling).toHaveClass('room-timeline');
    expect(errorSlot?.nextElementSibling?.nextElementSibling).toHaveClass('room-composer-dock');
    expect(screen.getByTestId('virtuoso-list')).toHaveAttribute('data-initial-index', 'LAST');
    expect(screen.getByTestId('virtuoso-list')).toHaveAttribute('data-initial-align', 'end');
    expect(screen.getByTestId('virtuoso-list')).not.toHaveAttribute('data-align-to-bottom');
    await user.type(composer, '并行核对边界');
    await user.click(screen.getByRole('button', { name: '立即干预当前回合' }));
    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.room.participant.steer')).toBe(true));
    const request = transport.requests.find((call) => call.request.pathId === 'agent.room.participant.steer')?.request;
    expect(request?.params).toEqual({ roomId: 'room-a' });
    expect(request?.body).toMatchObject({
      action: 'steer_participant',
      message: '并行核对边界',
      rootId: 'room-a:turn-1',
    });
    expect(request?.body).not.toHaveProperty('workItemId');
    expect(transport.subscriptionCalls[0]?.request).toMatchObject({
      pathId: 'agent.room.events',
      params: { roomId: 'room-a' },
      lastEventId: 'room-a:1',
    });
  });

  it('steers the explicitly mentioned partner during an active Room turn', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '点名干预')] },
      'agent.room.snapshot': roomSnapshot('room-a', [
        roomEvent('room-a', 1, 'user_message', { text: '正在执行的任务' }),
      ], '点名干预'),
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.participant.steer': { ok: true },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    await user.type(composer, '@Mars 先核对证据');
    await user.click(screen.getByRole('button', { name: '立即干预当前回合' }));

    await waitFor(() => expect(transport.requests.some((call) => (
      call.request.pathId === 'agent.room.participant.steer'
    ))).toBe(true));
    expect(transport.requests.find((call) => (
      call.request.pathId === 'agent.room.participant.steer'
    ))?.request.body).toMatchObject({
      message: '@Mars 先核对证据',
      participantId: 'room-a:p2',
      rootId: 'room-a:turn-1',
    });
  });

  it('keeps an active-turn draft when more than one partner is mentioned', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '点名干预')] },
      'agent.room.snapshot': roomSnapshot('room-a', [
        roomEvent('room-a', 1, 'user_message', { text: '正在执行的任务' }),
      ], '点名干预'),
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.participant.steer': { ok: true },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    await user.type(composer, '@Earth @Mars 分别调整');
    expect(composer).toHaveValue('@Earth @Mars 分别调整');
    await user.click(screen.getByRole('button', { name: '立即干预当前回合' }));

    await waitFor(() => expect(composer).toHaveValue('@Earth @Mars 分别调整'));
    expect(transport.requests
      .filter((call) => call.request.pathId === 'agent.room.participant.steer')
      .map((call) => call.request.body)).toEqual([]);
    expect(document.querySelector('.room-error-slot')).toHaveTextContent('当前回合只能点名一位伙伴，请只保留一个 @伙伴。');
  });

  it('starts a new execution after refresh when the latest root is terminal despite an older stale running root', async () => {
    const staleRootId = 'room-a:stale-running';
    const latestRootId = 'room-a:latest-completed';
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '已完成的 Room')] },
      'agent.room.snapshot': roomSnapshot('room-a', [
        roomEvent('room-a', 1, 'user_message', { text: '旧任务' }, { turnId: staleRootId }),
        roomEvent('room-a', 2, 'user_message', { text: '最新任务' }, { turnId: latestRootId }),
        roomEvent('room-a', 3, 'turn_completed', {
          rootId: latestRootId,
          dispatchId: 'room-a:latest-dispatch',
          status: 'completed',
        }, {
          turnId: latestRootId,
          participantId: 'room-a:p1',
          sourceSessionId: 'room-a:s1',
        }),
      ]),
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.message': { ok: true },
      'agent.room.participant.steer': { ok: true },
    } });
    const user = userEvent.setup();
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider><RoomsFeature /></TooltipProvider>
      </ControlTransportProvider>,
    );

    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    expect(screen.queryByText('当前任务仍在执行。现在发送文字会立即干预主持伙伴的当前回合。')).not.toBeInTheDocument();
    await user.type(composer, '开始下一项任务');
    await user.click(screen.getByRole('button', { name: '发送消息' }));

    await waitFor(() => expect(transport.requests.some((call) => (
      call.request.pathId === 'agent.room.message'
    ))).toBe(true));
    expect(transport.requests.some((call) => (
      call.request.pathId === 'agent.room.participant.steer'
    ))).toBe(false);
  });

  it('retries the read-only role catalog once before showing a Room warning', async () => {
    let roleAttempts = 0;
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '角色目录恢复')] },
      'agent.room.snapshot': roomSnapshot('room-a', []),
      'agent.roles.list': () => {
        roleAttempts += 1;
        if (roleAttempts === 1) throw new Error('gateway is still starting');
        return { ok: true, items: previewPersonas };
      },
    } });

    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await waitFor(() => expect(roleAttempts).toBe(2));
    expect(screen.queryByText(/角色目录暂时没有同步/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '重新同步角色' })).not.toBeInTheDocument();
  });

  it('keeps the normal composer after refresh when only detached subagent progress is unscoped', async () => {
    const rootId = 'room-a:root-completed';
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': {
        ok: true,
        items: [roomSummary('room-a', '已完成的嵌套协作')],
      },
      'agent.room.snapshot': roomSnapshot('room-a', [
        roomEvent('room-a', 1, 'user_message', {
          messageId: 'room-a:user-1',
          text: '运行嵌套子 Agent',
        }, { turnId: rootId }),
        roomEvent('room-a', 2, 'turn_completed', {
          rootId,
          dispatchId: 'room-a:dispatch-1',
          status: 'completed',
        }, {
          turnId: rootId,
          participantId: 'room-a:p1',
          sourceSessionId: 'room-a:s1',
        }),
        roomEvent('room-a', 3, 'participant_activity', {
          sourceEventId: 'room-a:s2:53',
          sourceEventType: 'tool_progress',
          data: {
            rootId: '',
            runId: 'subagent-run:1',
            state: 'completed',
            summary: '子 Agent 已返回结果，待主持会话核验',
            toolCallId: 'subagent:subagent-batch:1',
            toolName: 'agents',
          },
        }, {
          turnId: '',
          participantId: 'room-a:p2',
          sourceSessionId: 'room-a:s2',
        }),
      ]),
      'agent.roles.list': { ok: true, items: previewPersonas },
    } });

    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider><RoomsFeature /></TooltipProvider>
      </ControlTransportProvider>,
    );

    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    expect(composer).toHaveAttribute('placeholder', '继续聊，或输入 @ 请一位伙伴接手');
    expect(screen.queryByText('当前任务仍在执行。现在发送文字会立即干预主持伙伴的当前回合。')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '发送消息' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '立即干预当前回合' })).not.toBeInTheDocument();
  });

  it('automatically loads older Room history without collapsing an expanded report', async () => {
    const expandedMarker = '历史加载后仍保持展开。';
    const recent = [
      roomEvent('room-a', 3, 'user_message', { text: '最近消息三' }, {
        turnId: 'room-a:recent',
      }),
      roomTerminalPostEvent(
        'room-a',
        4,
        'result',
        `${'展开后保持可见。'.repeat(60)}${expandedMarker}`,
      ),
    ];
    const older = [
      roomEvent('room-a', 1, 'user_message', { text: '较早消息一' }, {
        turnId: 'room-a:older-1',
      }),
      roomEvent('room-a', 2, 'user_message', { text: '较早消息二' }, {
        turnId: 'room-a:older-2',
      }),
    ];
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '分页 Room')] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': roomSnapshot('room-a', recent),
      'agent.room.history': {
        schemaVersion: 'rag-ime.agent-room-event-page.v1',
        ok: true,
        roomId: 'room-a',
        items: older,
        firstSequence: 1,
        lastSequence: 2,
        nextBeforeSequence: 0,
        hasMore: false,
        retainedFirstSequence: 1,
        retainedLastSequence: 4,
        retainedPrefixTruncated: false,
      },
    } });
    const user = userEvent.setup();
    const { container } = render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    expect(await screen.findByText('最近消息三')).toBeInTheDocument();
    const report = container.querySelector<HTMLDetailsElement>('.room-agent-lane__report')!;
    expect(report).toHaveAttribute('open');
    expect(within(report).getByText(/历史加载后仍保持展开/)).toBeInTheDocument();
    expect(report).toHaveAttribute('open');
    await user.click(screen.getByRole('button', { name: '模拟到达历史顶部' }));
    expect(await screen.findByText('较早消息一')).toBeInTheDocument();
    expect(screen.getByText('较早消息二')).toBeInTheDocument();
    const reportAfterPrepend = container.querySelector<HTMLDetailsElement>('.room-agent-lane__report')!;
    expect(reportAfterPrepend).toBe(report);
    expect(within(reportAfterPrepend).getByText(/历史加载后仍保持展开/)).toBeInTheDocument();
    expect(reportAfterPrepend).toHaveAttribute('open');
    expect(screen.queryByRole('button', { name: '载入更早记录' })).not.toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: '协作时间线导航' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '查看最新进展' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '查看任务总览' })).toBeInTheDocument();
    expect(transport.requests.find((call) => call.request.pathId === 'agent.room.history')?.request).toMatchObject({
      params: { roomId: 'room-a' },
      query: { beforeSequence: 3, limit: 200 },
    });
  });

  it('follows a newly appended Room turn only while the reader remains at the live edge', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '续跑 Room')] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': roomSnapshot('room-a', [
        roomEvent('room-a', 1, 'user_message', { text: '旧任务' }, {
          turnId: 'room-a:turn-1',
        }),
      ]),
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    expect(await screen.findByText('旧任务')).toBeInTheDocument();
    const scroller = roomVirtuosoMock.scroller!;
    Object.defineProperty(scroller, 'scrollHeight', { configurable: true, value: 960 });
    expect(scroller.querySelector('.room-timeline__footer-space')).toBeInTheDocument();
    act(() => roomVirtuosoMock.atBottomStateChange?.(true));
    roomVirtuosoMock.scrollToIndex.mockClear();
    roomVirtuosoMock.scrollTo.mockClear();

    act(() => {
      useRoomLiveStore.getState().applyEvents('room-a', [
        parseRoomEvent(roomEvent('room-a', 2, 'user_message', { text: '新一轮任务' }, {
          turnId: 'room-a:turn-2',
        })),
      ]);
    });
    expect(await screen.findByText('新一轮任务')).toBeInTheDocument();
    await waitFor(() => expect(roomVirtuosoMock.scrollToIndex).toHaveBeenLastCalledWith({
      align: 'end',
      behavior: 'auto',
      index: 1,
    }));
    await waitFor(() => expect(roomVirtuosoMock.scrollTo).toHaveBeenLastCalledWith({
      behavior: 'auto',
      top: 960,
    }));

    roomVirtuosoMock.scrollToIndex.mockClear();
    roomVirtuosoMock.scrollTo.mockClear();
    act(() => {
      roomVirtuosoMock.scroller?.dispatchEvent(new WheelEvent('wheel', { deltaY: -24 }));
      useRoomLiveStore.getState().applyEvents('room-a', [
        parseRoomEvent(roomEvent('room-a', 3, 'user_message', { text: '后台新增但不抢位置' }, {
          turnId: 'room-a:turn-3',
        })),
      ]);
    });
    expect(await screen.findByText('后台新增但不抢位置')).toBeInTheDocument();
    await new Promise((resolve) => window.setTimeout(resolve, 20));
    expect(roomVirtuosoMock.scrollToIndex).not.toHaveBeenCalled();
    expect(roomVirtuosoMock.scrollTo).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: '查看最新进展' }));
    await waitFor(() => expect(roomVirtuosoMock.scrollTo).toHaveBeenLastCalledWith({
      behavior: 'auto',
      top: 960,
    }));
  });

  it('shows real Pi Session work in the task view when no explicit WorkItem exists', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '轻量 Room')] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.session.workflow.get': roomWorkflowFixture('room-a:s1'),
      'agent.subagents.list': roomSubagentListFixture('room-a:s1'),
      'agent.room.snapshot': roomSnapshot('room-a', [
        roomEvent('room-a', 1, 'user_message', { text: '修好伪空的任务界面' }, {
          turnId: 'room-a:turn-1',
        }),
        roomEvent('room-a', 2, 'participant_activity', {
          rootId: 'room-a:turn-1',
          dispatchId: 'room-a:dispatch-1',
          sourceEventType: 'tool_started',
          status: 'running',
          summary: '正在读取真实 Room 事件',
        }, {
          turnId: 'room-a:turn-1',
          participantId: 'room-a:p1',
          sourceSessionId: 'room-a:s1',
        }),
      ]),
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    expect(await screen.findByText('修好伪空的任务界面')).toBeInTheDocument();
    await user.click(screen.getByRole('radio', { name: '任务' }));

    const cockpit = screen.getByRole('region', { name: '任务流转与验收' });
    expect(cockpit).toHaveTextContent('修好伪空的任务界面');
    expect(cockpit).toHaveTextContent('进行中');
    expect(cockpit).toHaveTextContent('1 位伙伴已进入当前执行线');
    expect(cockpit).toHaveTextContent('1 个工具步骤');
    expect(cockpit).toHaveTextContent(/计划\s*1\/3/);
    expect(cockpit).toHaveTextContent('实现交互结果视图');
    expect(cockpit).toHaveTextContent('验证 HTML 报告');
    expect(cockpit).toHaveTextContent('子 Agent');
    expect(screen.getByRole('img', { name: /个节点、.*条任务关系的有向图/ })).toBeInTheDocument();
    expect(screen.queryByText(/还没有单独登记的工作项/)).not.toBeInTheDocument();
  });

  it('mounts and removes Room projection modules without changing Runtime state', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '模块化 Room')] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.session.workflow.get': roomWorkflowFixture('room-a:s1'),
      'agent.subagents.list': roomSubagentListFixture('room-a:s1'),
      'agent.room.snapshot': roomSnapshot('room-a', [
        roomEvent('room-a', 1, 'user_message', { text: '验证模块组合' }, {
          turnId: 'room-a:turn-1',
        }),
        roomEvent('room-a', 2, 'participant_activity', {
          rootId: 'room-a:turn-1',
          dispatchId: 'room-a:dispatch-1',
          sourceEventType: 'tool_started',
          status: 'running',
          summary: '正在投影模块',
        }, {
          turnId: 'room-a:turn-1',
          participantId: 'room-a:p1',
          sourceSessionId: 'room-a:s1',
        }),
      ]),
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('radio', { name: '任务' }));
    expect(screen.getByRole('heading', { name: '任务 Workflow' })).toBeInTheDocument();
    const moduleSummary = screen.getByText('模块').closest('summary')!;
    const moduleDisclosure = moduleSummary.closest('details')!;
    expect(moduleDisclosure).toHaveClass('ui-disclosure');
    expect(moduleSummary).toHaveAttribute('aria-expanded', 'false');
    await user.click(moduleSummary);
    expect(moduleSummary).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('组合投影，而不是复制 Runtime')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '移出阶段与并行流转' }));
    expect(screen.queryByRole('heading', { name: '任务 Workflow' })).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '验证模块组合' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '装载阶段与并行流转' }));
    expect(screen.getByRole('heading', { name: '任务 Workflow' })).toBeInTheDocument();
  }, 30_000);

  it('reuses the Session subagent launch policy inside Room with an explicit parent', async () => {
    const room = roomSummary('room-a', '子 Agent 配置 Room');
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [room] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': roomSnapshot('room-a', [
        roomEvent('room-a', 1, 'user_message', { text: '配置伙伴的子 Agent' }, {
          turnId: 'room-a:turn-1',
        }),
        roomEvent('room-a', 2, 'participant_activity', {
          rootId: 'room-a:turn-1',
          dispatchId: 'room-a:dispatch-1',
          sourceEventType: 'tool_started',
          status: 'running',
          summary: '主持伙伴正在分工',
        }, {
          turnId: 'room-a:turn-1',
          participantId: 'room-a:p1',
          sourceSessionId: 'room-a:s1',
        }),
      ]),
      'agent.subagents.templates': { ok: true, items: previewTemplates },
      'agent.tools.list': { ok: true, items: [] },
    } });
    const user = userEvent.setup();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <ControlTransportProvider transport={transport}>
        <QueryClientProvider client={queryClient}>
          <TooltipProvider><RoomsFeature /></TooltipProvider>
        </QueryClientProvider>
      </ControlTransportProvider>,
    );

    await user.click(await screen.findByRole('radio', { name: '任务' }));
    expect(transport.requests.some((call) => call.request.pathId === 'agent.subagents.templates')).toBe(false);
    await user.click(screen.getByText('配置子 Agent'));

    const parent = await screen.findByRole('combobox', { name: '子 Agent 父 Session' });
    expect(parent).toHaveValue('room-a:s1');
    expect(within(parent).getByRole('option', { name: /Earth · Root 主持/ })).toBeInTheDocument();
    expect(within(parent).getByRole('option', { name: /Mars/ })).toBeInTheDocument();
    expect(await screen.findByRole('radio', { name: /^审阅者/ })).toBeInTheDocument();
    await user.click(screen.getByText('执行者', { selector: 'strong' }));
    expect(screen.getByRole('radio', { name: '工作区写入' })).toBeChecked();
    const policyDetail = screen.getByText(/卫星 \/ Tool Agent 配置/);
    expect(policyDetail).toHaveTextContent('工作区托管');
    expect(policyDetail).toHaveTextContent('继承');
    expect(transport.requests.some((call) => call.request.pathId === 'agent.subagents.templates')).toBe(true);
  });

  it('keeps same-depth WorkItems as one assignment stage without inventing parallelism', async () => {
    const room = roomSummary('room-a', '阶段并行 Room');
    const first = roomWorkItemFixture(room.id, 'work:research', {
      objective: '并行调查运行契约',
      currentOwnerParticipantId: 'room-a:p1',
      accountableParticipantId: 'room-a:p1',
      state: 'active',
    });
    const second = roomWorkItemFixture(room.id, 'work:prototype', {
      objective: '并行制作交互原型',
      currentOwnerParticipantId: 'room-a:p2',
      accountableParticipantId: 'room-a:p2',
      state: 'active',
    });
    const handoff = roomWorkItemFixture(room.id, 'work:review', {
      rootWorkId: first.id,
      parentWorkId: first.id,
      depth: 2,
      objective: '接续复核两路交付',
      currentOwnerParticipantId: 'room-a:p1',
      accountableParticipantId: 'room-a:p1',
      state: 'queued',
    });
    room.workItems = [first, second, handoff];
    const snapshot = roomSnapshot(room.id, [
      roomEvent(room.id, 1, 'user_message', { text: '阶段并行完成 Room 重做' }),
      roomEvent(room.id, 2, 'participant_activity', {
        rootId: 'room-a:turn-1',
        dispatchId: 'dispatch:parent',
        sourceEventType: 'tool_started',
        status: 'running',
        summary: '主持伙伴正在整合',
      }, {
        participantId: 'room-a:p1',
        sourceSessionId: 'room-a:s1',
      }),
      roomEvent(room.id, 3, 'participant_activity', {
        activityKind: 'child',
        phase: 'started',
        parentParticipantId: 'room-a:p1',
        targetParticipantId: 'room-a:p2',
        parentDispatchId: 'dispatch:parent',
        childDispatchId: 'dispatch:partner-child',
        task: '复核并行结果',
      }, {
        participantId: 'room-a:p1',
        sourceSessionId: 'room-a:s1',
      }),
      roomEvent(room.id, 4, 'participant_activity', {
        activityKind: 'child',
        phase: 'completed',
        childDispatchId: 'dispatch:partner-child',
        status: 'completed',
        summary: '复核已返回',
      }, {
        participantId: 'room-a:p2',
        sourceSessionId: 'room-a:s2',
      }),
    ]);
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [room] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': {
        ...snapshot,
        room: { ...snapshot.room, workItems: room.workItems },
      },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('radio', { name: '任务' }));
    const cockpit = screen.getByRole('region', { name: '任务流转与验收' });
    expect(cockpit).toHaveTextContent('分工');
    expect(cockpit).toHaveTextContent('2 项分工');
    expect(cockpit).not.toHaveTextContent('2 路并行');
    expect(cockpit).toHaveTextContent('接续执行');
    expect(cockpit).toHaveTextContent('接续复核两路交付');
    const workflow = within(cockpit).getByRole('region', { name: '当前任务 Workflow 图' });
    expect(within(workflow).getAllByText('分派')).toHaveLength(2);
    expect(within(workflow).getByText('依赖')).toBeInTheDocument();
    expect(within(cockpit).getByRole('heading', { name: /Partner 调用/ })).toBeInTheDocument();
    expect(cockpit).toHaveTextContent('@ Earth → @ Mars');
    expect(cockpit).toHaveTextContent('复核并行结果');
    expect(cockpit).toHaveTextContent('复核已返回');
    expect(within(cockpit).getAllByRole('button', { name: /执行节点/ })).toHaveLength(3);
    await user.click(within(cockpit).getByRole('button', { name: /接续复核两路交付/ }));
    expect(within(cockpit).getByRole('region', { name: '@ Earth 的节点详情' })).toHaveTextContent('接续复核两路交付');
  });

  it('marks only a real Room wave as parallel and exposes its delivery contract', async () => {
    const room = roomSummary('room-a', '真实并行 Room');
    const waveId = 'room-wave:test-1';
    const rootId = 'room-a:turn-wave';
    const snapshot = roomSnapshot(room.id, [
      roomEvent(room.id, 1, 'user_message', { text: '并行核对运行时与界面' }, { turnId: rootId }),
      roomEvent(room.id, 2, 'participant_activity', {
        rootId,
        activityKind: 'child',
        phase: 'started',
        phaseName: '并行调查',
        waveId,
        parallelIndex: 0,
        parallelSize: 2,
        parentParticipantId: 'room-a:p2',
        targetParticipantId: 'room-a:p1',
        dispatchId: 'dispatch:wave:runtime',
        childDispatchId: 'dispatch:wave:runtime',
        task: '核对运行时契约',
        expectedOutput: '契约证据',
        acceptanceCriteria: ['给出调用链'],
      }, {
        turnId: rootId,
        participantId: 'room-a:p1',
        sourceSessionId: 'room-a:s1',
      }),
      roomEvent(room.id, 3, 'participant_activity', {
        rootId,
        activityKind: 'child',
        phase: 'started',
        phaseName: '并行调查',
        waveId,
        parallelIndex: 1,
        parallelSize: 2,
        parentParticipantId: 'room-a:p1',
        targetParticipantId: 'room-a:p2',
        dispatchId: 'dispatch:wave:ui',
        childDispatchId: 'dispatch:wave:ui',
        task: '核对前端投影',
        expectedOutput: '投影证据',
        acceptanceCriteria: ['给出事件字段'],
      }, {
        turnId: rootId,
        participantId: 'room-a:p2',
        sourceSessionId: 'room-a:s2',
      }),
      roomEvent(room.id, 4, 'participant_activity', {
        rootId,
        activityKind: 'child',
        phase: 'completed',
        phaseName: '并行调查',
        waveId,
        parallelIndex: 0,
        parallelSize: 2,
        targetParticipantId: 'room-a:p1',
        dispatchId: 'dispatch:wave:runtime',
        childDispatchId: 'dispatch:wave:runtime',
        status: 'completed',
        summary: '运行时契约已核对',
      }, {
        turnId: rootId,
        participantId: 'room-a:p1',
        sourceSessionId: 'room-a:s1',
      }),
    ]);
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [room] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': snapshot,
    } });
    const user = userEvent.setup();
    const { container } = render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider><RoomsFeature /></TooltipProvider>
      </ControlTransportProvider>,
    );

    await user.click(await screen.findByRole('radio', { name: '任务' }));
    const cockpit = screen.getByRole('region', { name: '任务流转与验收' });
    expect(cockpit).toHaveTextContent('并行调查');
    expect(cockpit).toHaveTextContent('2 路并行');
    expect(container.querySelector('.room-cockpit__flow-phase')).toHaveAttribute('data-parallel', 'true');
    await user.click(within(cockpit).getByRole('button', { name: /核对运行时契约/ }));
    const inspector = within(cockpit).getByRole('region', { name: '@ Earth 的节点详情' });
    expect(inspector).toHaveTextContent('契约证据');
    expect(inspector).toHaveTextContent('给出调用链');
    expect(inspector).toHaveTextContent('运行时契约已核对');
  });

  it('navigates authoritative WorkItem documents and Knowledge evidence', async () => {
    const room = roomSummary('room-a', '文档导航 Room');
    const work = roomWorkItemFixture(room.id, 'work:documented', {
      objective: '落实文档导航',
      state: 'active',
      evidenceRefs: ['knowledge:room-runtime-contract'],
    });
    room.workItems = [work];
    const document = roomWorkDocumentFixture(work.id, 'WorkItem 执行说明');
    const unrelated = roomWorkDocumentFixture('work:other', '不相关文档');
    const snapshot = roomSnapshot(room.id, []);
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [room] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': {
        ...snapshot,
        room: { ...snapshot.room, workItems: room.workItems },
      },
      'workDocuments.list': {
        schemaVersion: 'rag-ime.work-document-list.v1',
        items: [document, unrelated],
        total: 2,
      },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('radio', { name: '任务' }));
    const workflow = screen.getByRole('region', { name: '当前任务 Workflow 图' });
    expect(within(workflow).getByRole('img', { name: /个节点、.*条任务关系的有向图/ })).toBeInTheDocument();
    expect(workflow).toHaveTextContent('实线箭头来自真实 WorkItem 父子依赖');
    expect(workflow).toHaveTextContent('WorkItem 执行说明');
    const documentLinks = await screen.findAllByRole('link', { name: /WorkItem 执行说明/ });
    expect(documentLinks[0]).toHaveAttribute(
      'href',
      `#/work-documents?document=${encodeURIComponent(document.documentId)}`,
    );
    expect(screen.getByText(/对应 Session room-a:s1/)).toBeInTheDocument();
    const peerEvidence = screen.getByText('直接 @ 通信证据').closest('button');
    expect(peerEvidence).toHaveAttribute('aria-expanded', 'true');
    expect(screen.queryByText('不相关文档')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: /room-runtime-contract/ })).toHaveAttribute(
      'href',
      '#/knowledge',
    );
  });

  it('keeps an archived WorkItem document on the completed Workflow node', async () => {
    const room = roomSummary('room-a', '已完成文档 Room');
    const work = roomWorkItemFixture(room.id, 'work:archived-document', {
      objective: '完成并归档工作文档',
      state: 'done',
      completedAtMs: 3,
    });
    const activeDocument = roomWorkDocumentFixture(work.id, '已归档的执行说明');
    const archivedDocument: WorkDocumentV1 = {
      ...activeDocument,
      state: 'archived',
      path: activeDocument.archivePath,
    };
    work.evidenceRefs = [`workdoc:${archivedDocument.documentId}@2`];
    room.workItems = [work];
    const snapshot = roomSnapshot(room.id, []);
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [room] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': {
        ...snapshot,
        room: { ...snapshot.room, workItems: room.workItems },
      },
      'workDocuments.list': {
        schemaVersion: 'rag-ime.work-document-list.v1',
        items: [],
        total: 0,
      },
      'workDocuments.history.search': {
        schemaVersion: 'rag-ime.work-document-list.v1',
        items: [archivedDocument],
        total: 1,
      },
      'workDocuments.get': {
        schemaVersion: 'rag-ime.work-document-detail.v1',
        document: archivedDocument,
        reopen: {
          eligible: false,
          authorityRevision: archivedDocument.authorityRevision,
          transitionReceiptId: '',
          reasonCode: 'authority_terminal',
        },
      },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('radio', { name: '任务' }));
    const workflow = screen.getByRole('region', { name: '当前任务 Workflow 图' });
    expect(workflow).toHaveTextContent('已归档的执行说明');
    expect(workflow).not.toHaveTextContent('文档 · 尚未登记');
    const documentLinks = await screen.findAllByRole('link', { name: /已归档的执行说明/ });
    expect(documentLinks[0]).toHaveAttribute(
      'href',
      `#/work-documents?document=${encodeURIComponent(archivedDocument.documentId)}&scope=history`,
    );
    expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'workDocuments.get',
        params: { documentId: archivedDocument.documentId },
      }),
    }));
  });

  it('keeps Knowledge-only evidence visible without a Todo, document, or artifact', async () => {
    const room = roomSummary('room-a', 'Knowledge 导航 Room');
    const work = roomWorkItemFixture(room.id, 'work:knowledge-only', {
      objective: '核对持久化知识来源',
      state: 'active',
      evidenceRefs: ['knowledge:knowledge-only'],
    });
    room.workItems = [work];
    const snapshot = roomSnapshot(room.id, []);
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [room] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': {
        ...snapshot,
        room: { ...snapshot.room, workItems: room.workItems },
      },
      'workDocuments.list': {
        schemaVersion: 'rag-ime.work-document-list.v1',
        items: [],
        total: 0,
      },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('radio', { name: '任务' }));
    expect(await screen.findByRole('link', { name: /knowledge-only/ })).toHaveAttribute(
      'href',
      '#/knowledge',
    );
  });

  it('keeps a failed Partner child Agent local instead of failing the whole Room', async () => {
    const subagents = roomSubagentListFixture('room-a:s1');
    subagents.items[0]!.runs[0]!.state = 'failed';
    subagents.items[0]!.runs[0]!.error = '子 Agent 校验没有通过';
    subagents.items[0]!.state = 'failed';
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '子 Agent Room')] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.session.workflow.get': roomWorkflowFixture('room-a:s1'),
      'agent.subagents.list': subagents,
      'agent.room.snapshot': roomSnapshot('room-a', [
        roomEvent('room-a', 1, 'user_message', { text: '显示伙伴调用的子 Agent' }, {
          turnId: 'room-a:turn-1',
        }),
        roomEvent('room-a', 2, 'participant_activity', {
          rootId: 'room-a:turn-1',
          dispatchId: 'room-a:dispatch-1',
          sourceEventType: 'tool_started',
          status: 'running',
          summary: '父伙伴仍在继续整合',
        }, {
          turnId: 'room-a:turn-1',
          participantId: 'room-a:p1',
          sourceSessionId: 'room-a:s1',
        }),
      ]),
    } });
    const user = userEvent.setup();
    const { container } = render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    expect(await screen.findByText('显示伙伴调用的子 Agent')).toBeInTheDocument();
    await user.click(screen.getByRole('radio', { name: '任务' }));

    expect(await screen.findByText('局部失败：')).toBeInTheDocument();
    expect(screen.getByText('子 Agent 校验没有通过')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '打开“验证 HTML 报告”子 Agent 对话' })).toHaveAttribute(
      'href',
      '#/agent?session=room-a%3As2%3Achild-1',
    );
    expect(container.querySelector('.room-cockpit')).toHaveAttribute('data-state', 'active');
    expect(container.querySelector('.room-cockpit__subagent')).toHaveAttribute('data-state', 'attention');
    expect(screen.queryByText('这轮协作没有完成')).not.toBeInTheDocument();
  });

  it('shows an invalid child delivery contract as a repairable local node', async () => {
    const subagents = roomSubagentListFixture('room-a:s1');
    const run = subagents.items[0]!.runs[0]!;
    run.state = 'completed';
    run.contract = {
      status: 'invalid',
      error: 'claims 必须是数组',
      toolCallId: '',
      validatedAtMs: null,
    };
    run.result = {
      deliveryStatus: 'returned',
      verificationStatus: 'contract_invalid',
      summary: '模型已返回普通文本',
    };
    run.completedAtMs = 4;
    subagents.items[0]!.state = 'completed';
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '合同修复 Room')] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.session.workflow.get': roomWorkflowFixture('room-a:s1'),
      'agent.subagents.list': subagents,
      'agent.room.snapshot': roomSnapshot('room-a', [
        roomEvent('room-a', 1, 'user_message', { text: '检查结构化交付合同' }),
        roomEvent('room-a', 2, 'participant_activity', {
          rootId: 'room-a:turn-1',
          dispatchId: 'room-a:dispatch-1',
          sourceEventType: 'tool_started',
          status: 'running',
          summary: '父伙伴继续修复交付',
        }, {
          participantId: 'room-a:p1',
          sourceSessionId: 'room-a:s1',
        }),
      ]),
    } });
    const user = userEvent.setup();
    const { container } = render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('radio', { name: '任务' }));
    expect(await screen.findByText('已返回但交付合同无效：')).toBeInTheDocument();
    expect(screen.getByText('claims 必须是数组')).toBeInTheDocument();
    expect(container.querySelector('.room-cockpit')).toHaveAttribute('data-state', 'active');
    expect(container.querySelector('.room-cockpit__subagent')).toHaveAttribute('data-state', 'contract');
    expect(screen.queryByText('这轮协作没有完成')).not.toBeInTheDocument();
  });

  it('does not fabricate a task graph before real work exists', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '空任务 Room')] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': roomSnapshot('room-a', [], '空任务 Room'),
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    expect(await screen.findByRole('button', { name: '打开协作空间：空任务 Room' })).toBeInTheDocument();
    await user.click(screen.getByRole('radio', { name: '任务' }));

    expect(screen.getByRole('region', { name: '任务流转与验收' })).toBeInTheDocument();
    expect(screen.getByText('还没有实际分工')).toBeInTheDocument();
    expect(screen.getByText('还没有实际流转')).toBeInTheDocument();
    expect(screen.getByText('只有产生真实 dispatch / WorkItem 后，Room 才会绘制节点与关系。')).toBeInTheDocument();
    expect(screen.queryByText('共同目标')).not.toBeInTheDocument();
    expect(screen.queryByText('等待任务拆分')).not.toBeInTheDocument();
  });

  it('shows real assignment, progress, delivery, and acceptance details for registered work', async () => {
    const room = roomSummary('room-a', '真实分工 Room');
    const work: NonNullable<RoomSummary['workItems']>[number] = {
      id: 'room-work:implementation',
      roomId: room.id,
      topicId: '',
      rootTurnId: 'room-a:turn-1',
      rootWorkId: 'room-work:implementation',
      parentWorkId: '',
      objective: '恢复任务图的真实详情',
      expectedOutput: '可展开核对的任务详情',
      acceptanceCriteria: ['空 Room 不画伪任务图', '显示负责人和当前进度'],
      accountableParticipantId: 'room-a:p1',
      currentOwnerParticipantId: 'room-a:p2',
      offeredToParticipantId: '',
      createdByParticipantId: 'room-a:p1',
      clientMessageId: 'test-work-detail',
      state: 'active',
      depth: 1,
      revision: 2,
      resultSummary: '已接入真实 WorkItem',
      artifactRefs: ['artifact:task-view'],
      evidenceRefs: ['evidence:task-view'],
      blocker: {},
      acceptedTurnId: 'room-a:turn-1',
      createdAtMs: 1,
      updatedAtMs: 2,
      completedAtMs: null,
    };
    room.workItems = [work];
    const snapshot = roomSnapshot(room.id, []);
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [room] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.session.workflow.get': roomWorkflowFixture('room-a:s2'),
      'agent.subagents.list': roomSubagentListFixture('room-a:s2'),
      'agent.room.snapshot': {
        ...snapshot,
        room: { ...snapshot.room, workItems: [work] },
      },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('radio', { name: '任务' }));

    const details = screen.getByRole('region', { name: '任务流转与验收' });
    expect(details).toHaveTextContent('0/1 位伙伴完成当前分工');
    expect(details).toHaveTextContent('恢复任务图的真实详情');
    expect(details).toHaveTextContent('进行中');
    expect(details).not.toHaveTextContent('执行人');
    await user.click(within(details).getByRole('button', { name: /查看分工依据与验收条件/ }));
    expect(details).toHaveTextContent('执行人');
    expect(details).toHaveTextContent('Mars');
    expect(details).toHaveTextContent('复核人');
    expect(details).toHaveTextContent('Earth');
    expect(details).toHaveTextContent('可展开核对的任务详情');
    expect(details).toHaveTextContent('空 Room 不画伪任务图');
    expect(details).toHaveTextContent('显示负责人和当前进度');
    expect(details).toHaveTextContent('第 2 次修订');
    expect(details).toHaveTextContent('已接入真实 WorkItem');
    expect(details).toHaveTextContent('/Volumes/work/learnA');
    expect(details).toHaveTextContent(/计划\s*1\/3/);
    expect(details).toHaveTextContent('实现交互结果视图');
    expect(details).toHaveTextContent('验证 HTML 报告');
    expect(details).toHaveTextContent('验证 HTML 报告');
    expect(details).toHaveTextContent('evidence:task-view');
    expect(details).toHaveTextContent('artifact:task-view');
    expect(within(details).getByRole('link', { name: '打开Mars的对话' })).toHaveAttribute(
      'href',
      '#/agent?session=room-a%3As2',
    );
    expect(within(details).getByRole('link', { name: '打开 @ Mars 对话' })).toHaveAttribute(
      'href',
      '#/agent?session=room-a%3As2',
    );
  });

  it('shows the canonical blocked WorkItem state instead of claiming work is executing', async () => {
    const room = roomSummary('room-a', '阻塞 Room');
    const blockedWork: NonNullable<RoomSummary['workItems']>[number] = {
      id: 'room-work:blocked',
      roomId: room.id,
      topicId: '',
      rootTurnId: 'room-root:blocked',
      rootWorkId: 'room-work:blocked',
      parentWorkId: '',
      objective: '核对失败证据',
      expectedOutput: '明确恢复步骤',
      acceptanceCriteria: ['不再显示执行中'],
      accountableParticipantId: 'room-a:p1',
      currentOwnerParticipantId: 'room-a:p2',
      offeredToParticipantId: '',
      createdByParticipantId: 'room-a:p1',
      clientMessageId: 'test-work-blocked',
      state: 'blocked',
      depth: 1,
      revision: 0,
      resultSummary: '',
      artifactRefs: [],
      evidenceRefs: [],
      blocker: {
        reason: 'Room 执行已阻塞。',
        nextStep: '检查阻塞证据。',
      },
      acceptedTurnId: 'room-root:blocked',
      createdAtMs: 1,
      updatedAtMs: 2,
      completedAtMs: null,
    };
    room.workItems = [blockedWork];
    const snapshot = roomSnapshot(room.id, []);
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [room] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': {
        ...snapshot,
        room: { ...snapshot.room, workItems: [blockedWork] },
      },
    } });

    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    expect(await screen.findByText('learnA · 已阻塞')).toBeInTheDocument();
    expect(screen.queryByText(/正在完成任务/)).not.toBeInTheDocument();
    expect(screen.getByText(/已阻塞 · Mars · 核对失败证据/)).toBeInTheDocument();
    const composer = screen.getByRole('textbox', { name: '协作消息' });
    expect(composer).toBeEnabled();
    await userEvent.type(composer, '请按阻塞证据继续');
    expect(screen.getByRole('button', { name: '告诉伙伴怎样继续' })).toBeEnabled();
    expect(screen.getByText(/当前任务已暂停/)).toBeInTheDocument();
  });


  it('submits the pending wait answer through the busy gate and clears it only from the accepted user RoomPost', async () => {
    const pendingSend = deferred<Record<string, unknown>>();
    const room = roomSummary('room-a', '澄清 Room');
    const activeWork: NonNullable<RoomSummary['workItems']>[number] = {
      id: 'room-work:alignment',
      roomId: room.id,
      topicId: '',
      rootTurnId: 'room-a:turn-1',
      rootWorkId: 'room-work:alignment',
      parentWorkId: '',
      objective: '对齐发布方式',
      expectedOutput: '用户确认',
      acceptanceCriteria: ['记录用户选择'],
      accountableParticipantId: 'room-a:p1',
      currentOwnerParticipantId: 'room-a:p1',
      offeredToParticipantId: '',
      createdByParticipantId: 'room-a:p1',
      clientMessageId: 'opening-message',
      state: 'active',
      depth: 0,
      revision: 1,
      resultSummary: '',
      artifactRefs: [],
      evidenceRefs: [],
      blocker: {},
      acceptedTurnId: 'room-a:turn-1',
      createdAtMs: 1,
      updatedAtMs: 1,
      completedAtMs: null,
    };
    const snapshot = roomSnapshot('room-a', [
      roomQuestionEvent('room-a', 1, {
        content: '我需要你选择发布方式。',
        prompt: '这次采用哪一种发布方式？',
        options: [
          { value: 'A', label: '方案 A', description: '先发布预览版本', recommended: true },
          { value: 'B', label: '方案 B', description: '直接发布稳定版本' },
        ],
      }),
    ]);
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [{ ...room, workItems: [activeWork] }] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': {
        ...snapshot,
        room: { ...snapshot.room, workItems: [activeWork] },
      },
      'agent.room.message': () => pendingSend.promise,
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    const questionCard = within(await screen.findByRole('region', {
      name: '需要回答：这次采用哪一种发布方式？',
    }));
    expect(questionCard.getByText('这次采用哪一种发布方式？')).toBeInTheDocument();
    expect(questionCard.getByText('推荐')).toBeInTheDocument();
    expect(screen.getByText(/当前任务正在等待你的回答/)).toBeInTheDocument();
    expect(questionCard.getByRole('button', { name: '发送回答' })).toBeDisabled();

    await user.click(questionCard.getByRole('radio', { name: '方案 B' }));
    expect(questionCard.getByRole('radio', { name: '方案 B' })).toBeChecked();
    const submit = questionCard.getByRole('button', { name: '发送回答' });
    fireEvent.click(submit);
    fireEvent.click(submit);

    await waitFor(() => expect(
      transport.requests.filter(({ request }) => request.pathId === 'agent.room.message'),
    ).toHaveLength(1));
    const request = transport.requests.find(
      ({ request: item }) => item.pathId === 'agent.room.message',
    )!.request;
    expect(request.body).toMatchObject({
      message: 'B',
      attachmentIds: [],
    });
    expect(request.body).not.toHaveProperty('participantIds');
    expect(screen.getByRole('region', { name: '需要回答：这次采用哪一种发布方式？' })).toBeInTheDocument();

    const clientMessageId = String((request.body as Record<string, unknown>).clientMessageId);
    pendingSend.resolve({
      ok: true,
      timelineEvents: [
        roomEvent('room-a', 2, 'user_message', {
          messageId: 'room-a:answer-message-1',
          clientMessageId,
          rootId: 'room-a:turn-1',
          text: 'B',
        }, { turnId: 'room-a:turn-1' }),
        roomUserPostEvent('room-a', 3, 'room-a:turn-1', clientMessageId, 'B'),
      ],
    });
    await waitFor(() => expect(
      within(screen.getByRole('region', { name: '需要回答：这次采用哪一种发布方式？' }))
        .queryByRole('button', { name: '发送回答' }),
    ).not.toBeInTheDocument());
    const resolvedQuestion = within(screen.getByRole('region', { name: '需要回答：这次采用哪一种发布方式？' }));
    const resolvedPrompt = resolvedQuestion.getByText('这次采用哪一种发布方式？');
    expect(resolvedQuestion.getByText('已收到回答')).toBeInTheDocument();
    expect(resolvedQuestion.getByText('回答保留在下一条用户消息中')).toBeInTheDocument();
    const answerMessage = document.querySelector('.room-user-message');
    if (!answerMessage) {
      throw new Error('expected the accepted answer to render as a chronological user message');
    }
    expect(answerMessage).toHaveTextContent('方案 B');
    expect(resolvedPrompt.compareDocumentPosition(answerMessage) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
    expect(document.querySelectorAll('.room-user-message')).toHaveLength(1);
    expect(screen.getByRole('button', { name: '立即干预当前回合' })).toBeDisabled();
    await waitFor(() => expect(screen.getByRole('textbox', { name: '协作消息' })).toHaveFocus());
  });


  it('renders and submits a text-only wait without inventing options', async () => {
    const activeWork = roomWorkItemFixture('room-a', 'room-work:alignment', { state: 'active' });
    const snapshot = roomSnapshot('room-a', [
      roomQuestionEvent('room-a', 1, {
        content: '请补充不能改变的边界。',
        prompt: '还有哪些实现边界必须保留？',
        options: [],
      }),
    ]);
    snapshot.room = { ...snapshot.room, workItems: [activeWork] };
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [{ ...roomSummary('room-a', '自由回答 Room'), workItems: [activeWork] }] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': snapshot,
      'agent.room.message': (request: ControlRequest) => {
        const body = request.body as Record<string, unknown>;
        return {
          ok: true,
          timelineEvents: [
            roomUserPostEvent(
              'room-a',
              2,
              'room-a:turn-1',
              String(body.clientMessageId),
              String(body.message),
            ),
          ],
        };
      },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    const questionCard = within(await screen.findByRole('region', {
      name: '需要回答：还有哪些实现边界必须保留？',
    }));
    expect(questionCard.getByText('还有哪些实现边界必须保留？')).toBeInTheDocument();
    expect(questionCard.queryByRole('radio')).not.toBeInTheDocument();
    const answer = questionCard.getByRole('textbox', { name: /你的回答/ });
    await user.type(answer, '请保留现有 API，并补充错误状态。');
    await user.click(questionCard.getByRole('button', { name: '发送回答' }));

    await waitFor(() => expect(
      transport.requests.filter(({ request }) => request.pathId === 'agent.room.message'),
    ).toHaveLength(1));
    const answerRequest = transport.requests.find(
      ({ request }) => request.pathId === 'agent.room.message',
    )?.request.body;
    expect(answerRequest).toMatchObject({
      message: '请保留现有 API，并补充错误状态。',
      attachmentIds: [],
      answerToPostId: 'room-a:question-1',
      answerToRootId: 'room-a:turn-1',
      workItemId: 'room-work:alignment',
    });
    await waitFor(() => expect(
      within(screen.getByRole('region', { name: '需要回答：还有哪些实现边界必须保留？' }))
        .queryByRole('button', { name: '发送回答' }),
    ).not.toBeInTheDocument());
    const resolvedQuestion = within(screen.getByRole('region', { name: '需要回答：还有哪些实现边界必须保留？' }));
    expect(resolvedQuestion.getByText('已收到回答')).toBeInTheDocument();
    expect(resolvedQuestion.getByText('回答保留在下一条用户消息中')).toBeInTheDocument();
    expect(screen.getByText('请保留现有 API，并补充错误状态。').closest('.room-user-message')).toBeInTheDocument();
    expect(document.querySelectorAll('.room-user-message')).toHaveLength(1);
  });

  it('ignores optimistic-shaped and unrelated user events until the same Root publishes the accepted answer', async () => {
    const question = roomQuestionEvent('room-a', 1, {
      content: '需要你的确认。',
      prompt: '采用哪一种发布方式？',
      options: [{ value: 'preview', label: '预览版' }, { value: 'stable', label: '稳定版' }],
    });
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '权威回答 Room')] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': roomSnapshot('room-a', [
        question,
        roomEvent('room-a', 2, 'user_message', {
          clientMessageId: 'optimistic-shaped',
          text: 'stable',
        }, { turnId: 'room-a:turn-1' }),
      ]),
    } });
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    expect(await screen.findByRole('region', { name: '需要回答：采用哪一种发布方式？' })).toBeInTheDocument();
    transport.emit(
      'agent.room.events',
      roomUserPostEvent('room-a', 3, 'room-a:unrelated-root', 'unrelated-answer', 'stable'),
    );
    await waitFor(() => expect(
      within(screen.getByRole('region', { name: '需要回答：采用哪一种发布方式？' }))
        .getByRole('button', { name: '发送回答' }),
    ).toBeInTheDocument());

    transport.emit(
      'agent.room.events',
      roomEvent('room-a', 4, 'user_message', {
        answerToPostId: 'room-a:question-1',
        clientMessageId: 'accepted-answer',
        displayText: '稳定版',
        messageId: 'room-a:user-answer-4',
        rootId: 'room-a:turn-1',
        text: 'stable',
      }, { turnId: 'room-a:turn-1' }),
    );
    await waitFor(() => expect(
      within(screen.getByRole('region', { name: '需要回答：采用哪一种发布方式？' }))
        .queryByRole('button', { name: '发送回答' }),
    ).not.toBeInTheDocument());
    expect(within(screen.getByRole('region', { name: '需要回答：采用哪一种发布方式？' })).getByText('回答保留在下一条用户消息中')).toBeInTheDocument();
    expect(screen.getByText('稳定版').closest('.room-user-message')).toBeInTheDocument();
    expect(document.querySelectorAll('.room-user-message')).toHaveLength(3);
  });
  it('answers grouped participant clarification in Room and waits for the durable resolution event', async () => {
    const groupedRequest = roomEvent('room-a', 1, 'participant_activity', {
      rootId: 'room-a:turn-1',
      dispatchId: 'room-a:dispatch-1',
      sourceEventId: 'room-a:s1:input:grouped-1',
      sourceEventType: 'user_input_required',
      requestId: 'input:grouped-1',
      requestKind: 'grouped_questions',
      method: 'editor',
      title: '需要你做几个选择',
      message: '请一起确认下面两件事，提交后伙伴会继续。',
      questions: [
        { id: 'scope', question: '先覆盖哪一部分？', options: ['核心流程', '完整流程'] },
        { id: 'review', question: '如何复核？', options: ['伙伴互查', '直接交付'] },
      ],
    }, {
      turnId: 'room-a:turn-1',
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
    });
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '协作确认')] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': roomSnapshot('room-a', [groupedRequest]),
      'agent.session.ui.resolve': { ok: true },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    const cardElement = await screen.findByRole('region', { name: '需要你做几个选择' });
    const card = within(cardElement);
    expect(cardElement.parentElement).toHaveClass('room-composer-dock');
    expect(document.querySelector('.room-composer')).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: '需要你做几个选择' })).not.toBeInTheDocument();
    expect(card.getByRole('group', { name: '1. 先覆盖哪一部分？' })).toBeInTheDocument();
    expect(card.getByRole('group', { name: '2. 如何复核？' })).toBeInTheDocument();
    await user.click(card.getByRole('radio', { name: '核心流程' }));
    await user.click(card.getByRole('radio', { name: '伙伴互查' }));
    await user.click(card.getByRole('button', { name: '一起提交' }));

    await waitFor(() => expect(
      transport.requests.filter(({ request }) => request.pathId === 'agent.session.ui.resolve'),
    ).toHaveLength(1));
    expect(transport.requests.find(({ request }) => request.pathId === 'agent.session.ui.resolve')?.request).toMatchObject({
      params: { sessionId: 'room-a:s1' },
      body: {
        requestId: 'input:grouped-1',
        value: JSON.stringify({ answers: { scope: '核心流程', review: '伙伴互查' } }),
        resolutionSource: 'direct_user',
      },
    });
    expect(screen.getByRole('region', { name: '需要你做几个选择' })).toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.room.message')).toHaveLength(0);

    transport.emit('agent.room.events', roomEvent('room-a', 2, 'participant_activity', {
      rootId: 'room-a:turn-1',
      dispatchId: 'room-a:dispatch-1',
      sourceEventId: 'room-a:s1:input:grouped-1:resolved',
      sourceEventType: 'user_input_required',
      requestId: 'input:grouped-1',
      requestKind: 'grouped_questions',
      method: 'editor',
      resolutionState: 'resolved',
      resolutionSource: 'direct_user',
    }, {
      turnId: 'room-a:turn-1',
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
    }));
    await waitFor(() => expect(
      screen.queryByRole('region', { name: '需要你做几个选择' }),
    ).not.toBeInTheDocument());
  });

  it('keeps an inline question visible across Escape and a full reload', async () => {
    const question = roomQuestionEvent('room-a', 1, {
      content: '请选择发布方式，回答后我会继续。',
      prompt: '发布预览版还是稳定版？',
      options: [
        { value: 'preview', label: '预览版' },
        { value: 'stable', label: '稳定版', recommended: true },
      ],
    });
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '重载 Room')] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': roomSnapshot('room-a', [question]),
    } });
    const user = userEvent.setup();
    const view = render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    const questionName = '需要回答：发布预览版还是稳定版？';
    expect(await screen.findByRole('region', { name: questionName })).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(screen.getByRole('region', { name: questionName })).toBeInTheDocument();
    expect(within(screen.getByLabelText('协作对话时间线')).getByText('请选择发布方式，回答后我会继续。')).toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.room.message')).toHaveLength(0);

    view.unmount();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);
    const reloadedCard = within(await screen.findByRole('region', { name: questionName }));
    expect(reloadedCard.getByText('推荐')).toBeInTheDocument();
    expect(reloadedCard.getByRole('button', { name: '发送回答' })).toBeDisabled();
  });

  it('does not reopen a resolved question or fabricate choices for a legacy wait post', async () => {
    const structured = roomQuestionEvent('room-a', 1, {
      content: '先选择一个发布方式。',
      prompt: '选择发布方式',
      options: [
        { value: 'A', label: '方案 A' },
        { value: 'B', label: '方案 B' },
      ],
    });
    const legacy = roomTerminalPostEvent(
      'room-a',
      3,
      'wait',
      '请直接在对话中说明希望怎样继续。',
    );
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '已回答 Room')] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': roomSnapshot('room-a', [
        structured,
        roomUserPostEvent('room-a', 2, 'room-a:turn-1', 'answered-question', 'B'),
        legacy,
      ]),
    } });
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    const timeline = within(screen.getByLabelText('协作对话时间线'));
    expect(await timeline.findByText('请直接在对话中说明希望怎样继续。')).toBeInTheDocument();
    expect(timeline.getByText('先选择一个发布方式。')).toBeInTheDocument();
    const answeredCard = within(timeline.getByRole('region', { name: '需要回答：选择发布方式' }));
    expect(answeredCard.getByText('回答保留在下一条用户消息中')).toBeInTheDocument();
    expect(timeline.getByText('方案 B').closest('.room-user-message')).toBeInTheDocument();
    expect(answeredCard.queryByRole('button', { name: '发送回答' })).not.toBeInTheDocument();
    expect(answeredCard.queryAllByRole('radio')).toHaveLength(0);
    expect(document.querySelectorAll('.room-question-option')).toHaveLength(0);
  });

  it('labels each terminal public post as a humane report', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '终态 Room')] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': roomSnapshot('room-a', [
        roomTerminalPostEvent('room-a', 1, 'result', '交付已经完成。'),
        roomTerminalPostEvent('room-a', 2, 'handoff', '工作已经转交。'),
        roomTerminalPostEvent('room-a', 3, 'wait', '正在等待必要信息。'),
        roomTerminalPostEvent('room-a', 4, 'blocked', '当前被外部条件阻塞。'),
      ]),
    } });
    const { container } = render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider><RoomsFeature /></TooltipProvider>
      </ControlTransportProvider>,
    );

    expect(await within(screen.getByLabelText('协作对话时间线')).findByText('交付已经完成。')).toBeInTheDocument();
    expect(screen.getByText('已交接给 Mars')).toBeInTheDocument();
    expect(Array.from(container.querySelectorAll('.room-agent-lane__post-kind')).map(
      (element) => element.textContent,
    )).toEqual(['最终答复', '交接说明', '等待说明', '遇到的问题']);
  });

  it('keeps a managed interactive HTML result attached to the Room final', async () => {
    const room = roomSummary('room-html-final', 'HTML 结果 Room');
    const sessionId = 'room-html-final:s1';
    const mediaId = 'media_room_html_final_01';
    const post = {
      schemaVersion: 'wisdom-weasel.room-post.v2',
      postId: 'room-post:html-final',
      roomId: room.id,
      rootId: 'room-html-final:turn-1',
      generation: 0,
      dispatchId: 'room-html-final:dispatch-1',
      authorActorRef: 'room-html-final:p1',
      kind: 'result',
      visibility: 'room',
      content: '最终结果已生成。',
      blocks: [{
        schemaVersion: 'rag-ime.agent-block.v1',
        id: 'block:html-final',
        type: 'file',
        status: 'completed',
        presentationKind: 'file',
        data: {
          mediaId,
          sessionId,
          fileName: 'room-final.html',
          mimeType: 'text/html',
          byteSize: 240,
          sha256: 'f'.repeat(64),
        },
        summary: '交互式 Room Final',
        source: { kind: 'tool', ref: 'tool:write-html' },
        visibility: 'room_post',
        digest: 'e'.repeat(64),
        ref: 'artifact:room-final-html',
        generation: 0,
      }],
      idempotencyKey: 'room-final-html',
      publicationSource: { kind: 'room_post', ref: 'tool:room-final' },
      createdAtMs: 1,
    };
    const projection = [
      parseRoomEvent(roomEvent(room.id, 1, 'room_post', { post }, {
        turnId: post.rootId,
        participantId: 'room-html-final:p1',
        sourceSessionId: sessionId,
      })),
      parseRoomEvent(roomEvent(room.id, 2, 'turn_completed', {
        dispatchId: post.dispatchId,
        status: 'completed',
      }, {
        turnId: post.rootId,
        participantId: 'room-html-final:p1',
        sourceSessionId: sessionId,
      })),
    ].reduce((state, event) => reduceRoomEvent(state, event).state, createRoomProjection(room.id));
    const finalHtml = '<!doctype html><h1>Room Final 已渲染</h1><button onclick="this.textContent=\'已交互\'">验证交互</button>';
    const transport = new MockControlTransport({ routes: {
      'agent.media.preview': {
        schemaVersion: 'rag-ime.agent-file-preview.v1',
        descriptor: {
          schemaVersion: 'rag-ime.agent-file-descriptor.v1',
          mediaId,
          sessionId,
          fileName: 'room-final.html',
          mimeType: 'text/html',
          byteSize: 240,
          sha256: 'f'.repeat(64),
          previewKind: 'html',
          language: 'html',
          contentUrl: `/api/agent/media/${mediaId}/content?sessionId=${encodeURIComponent(sessionId)}`,
        },
        content: finalHtml,
        previewByteSize: new TextEncoder().encode(finalHtml).byteLength,
        truncated: false,
      },
    } });
    const user = userEvent.setup();
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <RoomTurn turnId={post.rootId} room={room} projection={projection} personas={previewPersonas} />
          <FilePreviewHost />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    expect(screen.getByText('最终答复')).toBeInTheDocument();
    expect(screen.getByText('room-final.html')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '预览报告' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByTitle('room-final.html 交互预览')).toHaveAttribute('sandbox', expect.stringContaining('allow-scripts'));
  });

  it('keeps lifecycle transitions and preserves each chronological public post', () => {
    const room = roomSummary('room-a', '单一汇报 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-report');
    projection.turnsById['turn-report'] = {
      id: 'turn-report',
      rootId: 'turn-report',
      status: 'completed',
      messageIds: ['progress-post', 'wait-post', 'result-post'],
      activityIds: [],
      participantIds: ['room-a:p1'],
      dispatchIds: ['dispatch-report'],
      terminalDispatchIds: ['dispatch-report'],
      failedDispatchIds: [],
      abortedDispatchIds: [],
      terminalParticipantIds: ['room-a:p1'],
      failedParticipantIds: [],
      abortedParticipantIds: [],
      dispatchParticipantIds: { 'dispatch-report': 'room-a:p1' },
      createdAtMs: 1,
      updatedAtMs: 3,
    };
    for (const [index, message] of [
      { id: 'progress-post', text: '先同步一条公开进展' },
      { id: 'wait-post', text: '旧的等待说明', postKind: 'wait' as const },
      { id: 'result-post', text: '最终任务汇报', postKind: 'result' as const },
    ].entries()) {
      projection.messagesById[message.id] = {
        id: message.id,
        roomId: room.id,
        turnId: 'turn-report',
        participantId: 'room-a:p1',
        sourceSessionId: 'room-a:s1',
        role: 'assistant',
        status: 'completed',
        text: message.text,
        projectionKind: 'post',
        rootId: 'turn-report',
        dispatchId: 'dispatch-report',
        ...(message.postKind ? { postKind: message.postKind } : {}),
        createdAtMs: index + 1,
        completedAtMs: index + 1,
      };
    }

    const { container } = render(
      <RoomTurn turnId="turn-report" room={room} projection={projection} personas={previewPersonas} />,
    );

    expect(screen.getByText('先同步一条公开进展')).toBeInTheDocument();
    expect(screen.getByText('最终任务汇报')).toBeInTheDocument();
    expect(screen.getByText('旧的等待说明')).toBeInTheDocument();
    expect(screen.getByText('正在等待继续条件')).toBeInTheDocument();
    expect(Array.from(container.querySelectorAll('.room-agent-lane__post-kind')).map(
      (element) => element.textContent,
    )).toEqual(['进度更新', '等待说明', '最终答复']);
    expect(screen.queryByText('运行结论')).not.toBeInTheDocument();
    expect(container.querySelector('.room-turn__terminal')).not.toBeInTheDocument();
    expect(container.querySelector('.room-agent-lane')).toHaveAttribute('open');
  });

  it('preserves distinct conversational updates within one active dispatch', () => {
    const room = roomSummary('room-a', '工作轮 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-round');
    projection.turnsById['turn-round'] = {
      id: 'turn-round',
      rootId: 'turn-round',
      status: 'running',
      messageIds: ['progress-old', 'progress-current'],
      activityIds: [],
      participantIds: ['room-a:p1'],
      dispatchIds: ['dispatch-round'],
      terminalDispatchIds: [],
      failedDispatchIds: [],
      abortedDispatchIds: [],
      terminalParticipantIds: [],
      failedParticipantIds: [],
      abortedParticipantIds: [],
      dispatchParticipantIds: { 'dispatch-round': 'room-a:p1' },
      createdAtMs: 1,
      updatedAtMs: 2,
    };
    for (const [index, text] of ['旧的过程播报', '正在核对可观察结果'].entries()) {
      const id = index === 0 ? 'progress-old' : 'progress-current';
      projection.messagesById[id] = {
        id,
        roomId: room.id,
        turnId: 'turn-round',
        participantId: 'room-a:p1',
        sourceSessionId: 'room-a:s1',
        role: 'assistant',
        status: 'completed',
        text,
        projectionKind: 'post',
        rootId: 'turn-round',
        dispatchId: 'dispatch-round',
        postKind: 'progress',
        createdAtMs: index + 1,
        completedAtMs: index + 1,
      };
    }

    const { container } = render(
      <RoomTurn turnId="turn-round" room={room} projection={projection} personas={previewPersonas} />,
    );

    expect(screen.getByText('旧的过程播报')).toBeInTheDocument();
    expect(screen.getByText('正在核对可观察结果')).toBeInTheDocument();
    expect(container.querySelectorAll('.room-agent-lane__post')).toHaveLength(2);
  });



  it('uses only the runtime receipt for response provenance', () => {
    const room = roomSummary('room-a', '回复用量 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-usage');
    projection.turnsById['turn-usage'] = {
      id: 'turn-usage',
      rootId: 'turn-usage',
      status: 'completed',
      messageIds: ['post-usage'],
      activityIds: ['usage-activity'],
      participantIds: ['room-a:p1'],
      dispatchIds: ['dispatch-usage'],
      terminalDispatchIds: ['dispatch-usage'],
      terminalParticipantIds: ['room-a:p1'],
      dispatchParticipantIds: { 'dispatch-usage': 'room-a:p1' },
      createdAtMs: 1,
      updatedAtMs: 3,
    };
    projection.messagesById['post-usage'] = {
      id: 'post-usage',
      roomId: room.id,
      turnId: 'turn-usage',
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
      role: 'assistant',
      status: 'completed',
      text: '已完成缓存验证。',
      projectionKind: 'post',
      rootId: 'turn-usage',
      dispatchId: 'dispatch-usage',
      postKind: 'result',
      createdAtMs: 2,
      completedAtMs: 2,
      message: {
        schemaVersion: 'rag-ime.agent-message.v1',
        id: 'self-reported-post',
        sessionId: 'room-a:s1',
        turnId: 'turn-usage',
        role: 'assistant',
        status: 'completed',
        blocks: [],
        attachments: [],
        citations: [],
        createdAtMs: 2,
        provider: 'self-reported-provider',
        model: 'self-reported-model',
        usage: { input: 9, output: 9, cacheRead: 9, cacheWrite: 9, totalTokens: 45 },
      },
    };
    projection.activitiesById['usage-activity'] = {
      id: 'usage-activity',
      turnId: 'turn-usage',
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
      kind: 'participant_activity',
      status: 'completed',
      summary: '正在整理正式 Post',
      payload: {
        rootId: 'turn-usage',
        dispatchId: 'dispatch-usage',
        sourceEventType: 'message_completed',
        responsePostId: 'post-usage',
        runtimeTurnId: 'runtime:usage:1',
        provider: 'anthropic',
        model: 'claude-sonnet-4-5',
        usageReported: true,
        cacheUsageReported: true,
        usage: {
          input: 1_200,
          output: 80,
          cacheRead: 900,
          cacheWrite: 30,
          totalTokens: 2_210,
        },
      },
      createdAtMs: 3,
      updatedAtMs: 3,
    };

    const view = render(
      <RoomTurn turnId="turn-usage" room={room} projection={projection} personas={previewPersonas} />,
    );

    const usage = screen.getByLabelText(
      '输入 1200 tokens，输出 80 tokens，缓存读取 900 tokens，缓存写入 30 tokens',
    );
    expect(usage).toHaveAttribute('data-cache-hit', 'true');
    expect(usage).toHaveAttribute(
      'title',
      'input=1200, output=80, cacheRead=900, cacheWrite=30',
    );
    expect(screen.getByText('缓存命中 900')).toBeInTheDocument();
    expect(screen.getByText('anthropic · claude-sonnet-4-5')).toBeInTheDocument();
    expect(within(screen.getByLabelText('回复运行记录')).getByText('运行记录')).toBeInTheDocument();
    expect(screen.queryByText(/self-reported-provider|self-reported-model/)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: '查看本轮上下文' })).toHaveAttribute(
      'href',
      '#/context-debug?sessionId=room-a%3As1&turnId=runtime%3Ausage%3A1',
    );
    projection.activitiesById['usage-activity'] = {
      ...projection.activitiesById['usage-activity']!,
      payload: {
        ...projection.activitiesById['usage-activity']!.payload,
        cacheUsageReported: false,
      },
    };
    view.rerender(
      <RoomTurn turnId="turn-usage" room={room} projection={projection} personas={previewPersonas} />,
    );
    const noCacheEvidence = screen.getByLabelText(
      '输入 1200 tokens，输出 80 tokens，缓存用量未上报',
    );
    expect(noCacheEvidence).not.toHaveAttribute('data-cache-hit');
    expect(noCacheEvidence).toHaveAttribute(
      'title',
      'input=1200, output=80, cache=unreported',
    );
    expect(screen.getByText('缓存未上报')).toBeInTheDocument();


    projection.turnsById['turn-usage'] = {
      ...projection.turnsById['turn-usage']!,
      activityIds: [],
    };
    view.rerender(
      <RoomTurn turnId="turn-usage" room={room} projection={projection} personas={previewPersonas} />,
    );
    expect(screen.getByText('本条回复未上报 Token / 缓存用量')).toBeInTheDocument();
    expect(screen.getByText('模型 / Provider 未上报')).toBeInTheDocument();
    expect(screen.queryByText(/self-reported-provider|self-reported-model/)).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: '查看本轮上下文' })).not.toBeInTheDocument();
  });

  it('keeps a terminal blocked Post as the visible Room response without a synthetic conclusion', () => {
    const room = roomSummary('room-a', '阻塞结果 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-blocked');
    projection.turnsById['turn-blocked'] = {
      id: 'turn-blocked',
      rootId: 'turn-blocked',
      status: 'completed',
      messageIds: ['blocked-post'],
      activityIds: [],
      participantIds: ['room-a:p1'],
      dispatchIds: ['dispatch-blocked'],
      terminalParticipantIds: ['room-a:p1'],
      failedParticipantIds: [],
      abortedParticipantIds: [],
      terminalDispatchIds: ['dispatch-blocked'],
      failedDispatchIds: [],
      abortedDispatchIds: [],
      dispatchParticipantIds: { 'dispatch-blocked': 'room-a:p1' },
      createdAtMs: 1,
      updatedAtMs: 2,
    };
    projection.messagesById['blocked-post'] = {
      id: 'blocked-post',
      roomId: room.id,
      turnId: 'turn-blocked',
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
      role: 'assistant',
      status: 'completed',
      text: '缺少发布凭据，当前无法继续。',
      projectionKind: 'post',
      postKind: 'blocked',
      rootId: 'turn-blocked',
      dispatchId: 'dispatch-blocked',
      createdAtMs: 1,
      completedAtMs: 2,
    };

    const { container } = render(
      <RoomTurn turnId="turn-blocked" room={room} projection={projection} personas={previewPersonas} />,
    );

    expect(screen.getByText('缺少发布凭据，当前无法继续。')).toBeVisible();
    expect(screen.queryByText('运行结论')).not.toBeInTheDocument();
    expect(container.querySelector('.room-turn__terminal')).not.toBeInTheDocument();
    expect(container.querySelector('.room-agent-lane')).toHaveAttribute('data-outcome', 'blocked');
  });

  it('shows the complete long Root final without requiring another disclosure', () => {
    const room = roomSummary('room-a', '长汇报 Room');
    const projection = createRoomProjection(room.id);
    const longReport = [
      '只读验收完成，未修改任何文件。',
      '命令回执与边界检查均已通过；所有公开结果都保留在受管工作目录中。',
      '第一项检查覆盖入口、键盘交互、移动端断点和无障碍状态。',
      '第二项检查覆盖测试、静态语法、产物目录与最终验收责任。',
      `${'验收补充说明仍在继续。'.repeat(20)}仅在完整汇报中显示的最终标记。`,
    ].join('\n\n');
    projection.turnOrder.push('turn-report');
    projection.turnsById['turn-report'] = {
      id: 'turn-report',
      status: 'completed',
      messageIds: ['result-post'],
      activityIds: [],
      participantIds: ['room-a:p1'],
      terminalParticipantIds: ['room-a:p1'],
      createdAtMs: 1,
      updatedAtMs: 2,
    };
    projection.messagesById['result-post'] = {
      id: 'result-post',
      roomId: room.id,
      turnId: 'turn-report',
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
      role: 'assistant',
      status: 'completed',
      text: longReport,
      projectionKind: 'post',
      postKind: 'result',
      createdAtMs: 1,
      completedAtMs: 2,
    };
    const { container } = render(
      <RoomTurn turnId="turn-report" room={room} projection={projection} personas={previewPersonas} />,
    );
    const report = container.querySelector<HTMLDetailsElement>('.room-agent-lane__report')!;

    expect(report).toHaveAttribute('open');
    expect(within(report).getByText('最终答复')).toBeInTheDocument();
    expect(within(report).getAllByText(/只读验收完成/).length).toBeGreaterThan(0);
    expect(within(report).getByText(/最终标记/)).toBeInTheDocument();
  });

  it('keeps a superseded question in history and allows only the newest question to answer', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '实时澄清 Room')] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': roomSnapshot('room-a', []),
    } });
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);
    await screen.findByText('还没有公开消息');

    transport.emit('agent.room.events', roomQuestionEvent('room-a', 1, {
      content: '第一项澄清',
      prompt: '较早的问题',
      options: [{ value: 'A', label: 'A' }, { value: 'B', label: 'B' }],
    }));
    transport.emit('agent.room.events', roomQuestionEvent('room-a', 2, {
      content: '第二项澄清',
      prompt: '最新的问题',
      options: [{ value: 'C', label: 'C' }, { value: 'D', label: 'D' }],
    }));

    const latestCard = within(await screen.findByRole('region', { name: '需要回答：最新的问题' }));
    const supersededCard = within(screen.getByRole('region', { name: '需要回答：较早的问题' }));
    expect(latestCard.getAllByRole('radio')).toHaveLength(2);
    expect(latestCard.getByRole('button', { name: '发送回答' })).toBeInTheDocument();
    expect(supersededCard.getByText('这项问题已由后续问题替代。')).toBeInTheDocument();
    expect(supersededCard.queryByRole('radio')).not.toBeInTheDocument();
  });

  it('imports a pasted PNG into the Room owner and sends its managed receipt once', async () => {
    const transport = new MockControlTransport({
      importedFiles: [{
        id: 'media_room_attachment01',
        name: 'diagram.png',
        mimeType: 'image/png',
        byteSize: 128,
        roomId: 'room-a',
        sha256: 'a'.repeat(64),
      }],
      routes: {
        'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '图片 Room')] },
        'agent.roles.list': { ok: true, items: previewPersonas },
        'agent.room.snapshot': roomSnapshot('room-a', []),
        'agent.room.message': { ok: true },
      },
    });
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);
    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    const image = new File(['png'], 'diagram.png', { type: 'image/png' });
    fireEvent.paste(composer, {
      clipboardData: { files: [image], items: [], getData: () => '' },
    });
    expect(await screen.findByLabelText('移除 diagram.png')).toBeInTheDocument();
    expect(transport.imagePasteCalls).toEqual([expect.objectContaining({
      roomId: 'room-a',
      maxFiles: 1,
    })]);
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }));
    await waitFor(() => expect(
      transport.requests.filter(({ request }) => request.pathId === 'agent.room.message'),
    ).toHaveLength(1));
    const request = transport.requests.find(({ request }) => request.pathId === 'agent.room.message')?.request;
    expect(request?.body).toMatchObject({
      message: '请查看附件。',
      attachmentIds: ['media_room_attachment01'],
    });
  });

  it('keeps a late image import with the Room that started the paste', async () => {
    const pendingPaste = deferred<PickedFile[]>();
    const transport = new MockControlTransport({
      importedFiles: [],
      routes: {
        'agent.rooms.list': { ok: true, items: [roomSummary('room-a', 'Room A'), roomSummary('room-b', 'Room B')] },
        'agent.roles.list': { ok: true, items: previewPersonas },
        'agent.room.snapshot': (request: ControlRequest) => roomSnapshot(String(request.params?.roomId ?? ''), []),
      },
    });
    vi.spyOn(transport, 'pasteImages').mockImplementation(() => pendingPaste.promise);
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);
    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    fireEvent.paste(composer, {
      clipboardData: {
        files: [new File(['png'], 'late.png', { type: 'image/png' })],
        items: [],
        getData: () => '',
      },
    });
    await user.click(screen.getByRole('button', { name: '打开协作空间：Room B' }));
    pendingPaste.resolve([{
      id: 'media_room_late_image01',
      name: 'late.png',
      mimeType: 'image/png',
      byteSize: 128,
      roomId: 'room-a',
      sha256: 'b'.repeat(64),
    }]);
    await waitFor(() => expect(transport.pasteImages).toHaveBeenCalledTimes(1));
    expect(screen.queryByLabelText('移除 late.png')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '打开协作空间：Room A' }));
    expect(await screen.findByLabelText('移除 late.png')).toBeInTheDocument();
  });

  it('keeps newer attachment receipts ahead of restored receipts when a pending send fails', async () => {
    const pendingSend = deferred<{ ok: true }>();
    const submitted = [
      pickedRoomImage('media_room_submitted_a', 'submitted-a.png', 'a'),
      pickedRoomImage('media_room_submitted_b', 'submitted-b.png', 'b'),
    ];
    const addedWhilePending = [
      pickedRoomImage('media_room_submitted_a', 'newer-duplicate.png', 'c'),
      ...Array.from({ length: 6 }, (_, index) => pickedRoomImage(
        `media_room_new_image_${index + 2}`,
        `new-${index + 2}.png`,
        String(index + 2),
      )),
    ];
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '附件恢复 Room')] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': roomSnapshot('room-a', []),
      'agent.room.message': () => pendingSend.promise,
    } });
    vi.spyOn(transport, 'pasteImages')
      .mockResolvedValueOnce(submitted)
      .mockResolvedValueOnce(addedWhilePending);
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);
    const composer = await screen.findByRole('textbox', { name: '协作消息' });

    fireEvent.paste(composer, {
      clipboardData: {
        files: [new File(['png'], 'submitted.png', { type: 'image/png' })],
        items: [],
        getData: () => '',
      },
    });
    expect(await screen.findByLabelText('移除 submitted-a.png')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }));
    await waitFor(() => expect(
      transport.requests.filter(({ request }) => request.pathId === 'agent.room.message'),
    ).toHaveLength(1));

    fireEvent.change(composer, { target: { value: '下一条补充说明' } });
    fireEvent.paste(composer, {
      clipboardData: {
        files: [new File(['png'], 'newer.png', { type: 'image/png' })],
        items: [],
        getData: () => '',
      },
    });
    expect(await screen.findByLabelText('移除 new-7.png')).toBeInTheDocument();
    pendingSend.reject(new Error('send failed'));
    await screen.findByRole('alert');
    expect(composer).toHaveValue('下一条补充说明');

    const restored = within(screen.getByLabelText('待发送附件'))
      .getAllByRole('button')
      .map((button) => button.getAttribute('aria-label'));
    expect(restored).toEqual([
      '移除 newer-duplicate.png',
      '移除 new-2.png',
      '移除 new-3.png',
      '移除 new-4.png',
      '移除 new-5.png',
      '移除 new-6.png',
      '移除 new-7.png',
      '移除 submitted-b.png',
    ]);
  });

  it('shows the user message and routing lane before the server accepts the send', async () => {
    const pending = deferred<{ ok: true }>();
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '即时反馈 Room')] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': roomSnapshot('room-a', []),
      'agent.room.message': () => pending.promise,
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    await user.type(composer, '发送后不能空白等待');
    await user.click(screen.getByRole('button', { name: '发送消息' }));

    expect(screen.getByText('发送后不能空白等待')).toBeInTheDocument();
    expect(screen.getByText('正在选择伙伴')).toBeInTheDocument();
    expect(screen.getAllByText('正在发送').length).toBeGreaterThan(0);
    expect(composer).toHaveValue('');
    expect(transport.requests.some((call) => call.request.pathId === 'agent.room.message')).toBe(true);

    pending.resolve({ ok: true });
  });

  it('keeps task ingress AI-owned without a second manual start form', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '需求对齐 Room')] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': roomSnapshot('room-a', []),
    } });
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await screen.findByText('还没有公开消息');
    expect(screen.queryByRole('button', { name: '确认任务' })).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: '把任务说清楚' })).not.toBeInTheDocument();
    expect(screen.queryByText('先聊清楚再开工')).not.toBeInTheDocument();
    expect(transport.requests.some(
      ({ request }) => request.pathId === 'agent.room.workItem.create',
    )).toBe(false);
  });

  it('retains an in-flight Room turn while navigating between Rooms', async () => {
    const pendingSend = deferred<{ ok: true }>();
    const pendingRefresh = deferred<ReturnType<typeof roomSnapshot>>();
    let roomASnapshotCalls = 0;
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': {
        ok: true,
        items: [roomSummary('room-a', 'Room A'), roomSummary('room-b', 'Room B')],
      },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': (request: ControlRequest) => {
        if (request.params?.roomId === 'room-b') return roomSnapshot('room-b', []);
        roomASnapshotCalls += 1;
        return roomASnapshotCalls === 1 ? roomSnapshot('room-a', []) : pendingRefresh.promise;
      },
      'agent.room.message': () => pendingSend.promise,
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    await user.type(composer, '切换页面也要看得到我');
    await user.click(screen.getByRole('button', { name: '发送消息' }));
    expect(screen.getByText('切换页面也要看得到我')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '打开协作空间：Room B' }));
    await screen.findByText('还没有公开消息');
    await user.click(screen.getByRole('button', { name: '打开协作空间：Room A' }));

    expect(await screen.findByText('切换页面也要看得到我')).toBeInTheDocument();
    expect(screen.getByText('正在选择伙伴')).toBeInTheDocument();
    pendingRefresh.resolve(roomSnapshot('room-a', []));
    await waitFor(() => expect(roomASnapshotCalls).toBe(2));
    expect(screen.getByText('切换页面也要看得到我')).toBeInTheDocument();

    pendingSend.resolve({ ok: true });
  });

  it('deduplicates a rapid send and keeps late failure state inside its source Room', async () => {
    const pendingA = deferred<{ ok: true }>();
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': {
        ok: true,
        items: [roomSummary('room-a', 'Room A'), roomSummary('room-b', 'Room B')],
      },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': (request: ControlRequest) => roomSnapshot(
        String(request.params?.roomId ?? ''),
        [],
      ),
      'agent.room.message': (request: ControlRequest) => (
        request.params?.roomId === 'room-a' ? pendingA.promise : { ok: true }
      ),
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    await user.type(composer, 'A 只应发送一次');
    const send = screen.getByRole('button', { name: '发送消息' });
    fireEvent.click(send);
    fireEvent.click(send);
    expect(transport.requests.filter(({ request }) => (
      request.pathId === 'agent.room.message' && request.params?.roomId === 'room-a'
    ))).toHaveLength(1);

    await user.click(screen.getByRole('button', { name: '打开协作空间：Room B' }));
    const roomBComposer = await screen.findByRole('textbox', { name: '协作消息' });
    await user.type(roomBComposer, 'B 可以独立发送');
    await user.click(screen.getByRole('button', { name: '发送消息' }));
    await user.type(roomBComposer, 'B 的未发送草稿');
    expect(transport.requests.filter(({ request }) => (
      request.pathId === 'agent.room.message' && request.params?.roomId === 'room-b'
    ))).toHaveLength(1);

    pendingA.reject(new Error('late Room A rejection'));
    await waitFor(() => expect(roomBComposer).toHaveValue('B 的未发送草稿'));
    expect(screen.queryByText('A 只应发送一次')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '打开协作空间：Room A' }));
    expect(await screen.findByRole('textbox', { name: '协作消息' })).toHaveValue('A 只应发送一次');
    expect(await screen.findByRole('alert')).toHaveTextContent('消息暂时未发送，请稍后重试。');
  });

  it('removes an optimistic message and restores the draft when the real API rejects it', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '失败恢复协作空间')] },
      'agent.room.snapshot': roomSnapshot('room-a', []),
      'agent.room.message': () => {
        throw new Error('POST /api/agent/rooms/room-a/messages failed: receipt=/tmp/private.json');
      },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);
    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    await user.type(composer, '不要留下假的乐观消息');
    await user.click(screen.getByRole('button', { name: '发送消息' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('消息暂时未发送，请稍后重试。');
    expect(alert).not.toHaveTextContent('/api/');
    expect(alert).not.toHaveTextContent('receipt');
    expect(alert).not.toHaveTextContent('/tmp/');
    expect(composer).toHaveValue('不要留下假的乐观消息');
    expect(document.querySelector('.room-user-message')).not.toBeInTheDocument();
  });

  it('creates a user-configured Room through the returned role catalog and selects the real response', async () => {
    const userCreatedPersona = {
      ...previewPersonas[2]!,
      roleId: 'persona-morning-guide',
      version: '1',
      displayName: '澄·晨光',
      tagline: '先看清今天，再稳稳向前',
      selectableModes: ['assistant', 'coordinator'] as ['assistant', 'coordinator'],
    };
    const roleCatalog = [userCreatedPersona, previewPersonas[0]!];
    const created = {
      ...roomSummary('room-created', '发布前检查'),
      participants: roleCatalog.map((persona, index) => ({
        id: `room-created:p${index + 1}`,
        sessionId: `room-created:s${index + 1}`,
        roleId: persona.roleId,
        roleVersion: persona.version,
        displayName: persona.displayName,
        status: 'active',
        ordinal: index,
      })),
    };
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [] },
      'agent.roles.list': { ok: true, items: roleCatalog },
      'agent.sessions.list': { ok: true, items: [{ id: 'existing', mode: 'coordinator', workspaceRoots: ['/Volumes/work/learnA'] }] },
      'agent.rooms.create': { ok: true, room: created },
      'agent.room.snapshot': roomSnapshot('room-created', [], '发布前检查'),
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);
    const create = await screen.findByRole('button', { name: '开始新的协作' });
    await waitFor(() => expect(create).toBeEnabled());
    await user.click(create);
    await user.type(screen.getByRole('textbox', { name: '协作空间名称' }), '发布前检查');
    await user.click(screen.getByRole('button', { name: '启用全自动并开始协作' }));

    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.rooms.create')).toBe(true));
    const request = transport.requests.find((call) => call.request.pathId === 'agent.rooms.create')?.request;
    expect(request?.body).toEqual({
      title: '发布前检查',
      roomKind: 'collaboration',
      avatar: 'briefcase',
      description: '',
      scenarioPrompt: '',
      participants: [roleCatalog[1]!, roleCatalog[0]!].map((persona, index) => ({
        roleId: persona.roleId,
        roleVersion: persona.version,
        displayName: persona.displayName,
        collaborationRole: index === 0 ? 'coordinator' : 'implementer',
      })),
      routingConfig: {
        maxResponders: 1,
        naturalJitter: 0,
        fallbackParticipantId: '',
      },
      routingPolicy: 'parallel',
      workspaceRoots: ['/Volumes/work/learnA', '/'],
      permissionPolicy: {
        schemaVersion: 'rag-ime.room-permission-policy.v1',
        room: { executionMode: 'full_trust' },
        partner: { executionMode: 'inherit' },
        toolAgent: { executionMode: 'inherit' },
      },
      dangerousModeConfirmation: 'ENABLE_FULL_TRUST',
    });
    expect(await screen.findByRole('button', { name: '打开协作空间：发布前检查' })).toHaveAttribute('aria-current', 'true');
    expect(screen.queryByRole('dialog', { name: '开始一起做事' })).not.toBeInTheDocument();
  });

  it('submits explicit Partner and Tool Agent narrowing without widening either child', async () => {
    const created = roomSummary('room-narrowed', '收窄权限 Room');
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.sessions.list': { ok: true, items: [] },
      'agent.rooms.create': { ok: true, room: created },
      'agent.room.snapshot': roomSnapshot(created.id, [], created.title),
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('button', { name: '开始新的协作' }));
    await user.type(screen.getByRole('textbox', { name: '协作空间名称' }), '收窄权限 Room');
    await user.selectOptions(
      screen.getByRole('combobox', { name: '行星 / Partner配置模式' }),
      'per_action',
    );
    await user.selectOptions(
      screen.getByRole('combobox', { name: '卫星 / Tool Agent配置模式' }),
      'read_only',
    );
    await user.click(screen.getByRole('button', { name: '启用全自动并开始协作' }));

    await waitFor(() => expect(
      transport.requests.some((call) => call.request.pathId === 'agent.rooms.create'),
    ).toBe(true));
    const request = transport.requests.find(
      (call) => call.request.pathId === 'agent.rooms.create',
    )?.request;
    expect(request?.body).toMatchObject({
      permissionPolicy: {
        schemaVersion: 'rag-ime.room-permission-policy.v1',
        room: { executionMode: 'full_trust' },
        partner: { executionMode: 'per_action' },
        toolAgent: { executionMode: 'read_only' },
      },
      dangerousModeConfirmation: 'ENABLE_FULL_TRUST',
    });
    expect(request?.body).not.toHaveProperty('executionMode');
  });

  it('keeps the project optional and preselects a useful collaboration ensemble', async () => {
    const transport = new MockControlTransport({
      pickedFiles: [{ id: 'workspace', name: 'learnA', mimeType: 'inode/directory', byteSize: 0, path: '/Volumes/work/learnA' }],
      routes: {
        'agent.rooms.list': { ok: true, items: [] },
        'agent.roles.list': { ok: true, items: previewPersonas },
        'agent.sessions.list': { ok: true, items: [] },
      },
    });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('button', { name: '开始新的协作' }));
    await user.type(screen.getByRole('textbox', { name: '协作空间名称' }), '无项目协作');
    expect(screen.getByRole('button', { name: '启用全自动并开始协作' })).toBeEnabled();
    expect(screen.getByRole('radio', { name: /不预选项目/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /候选行星 1/ })).toHaveAccessibleName(/候选行星 1.*最终汇合与回复/);
    expect(screen.getByRole('checkbox', { name: /候选行星 1/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /候选行星 2/ })).toHaveAccessibleName(/候选行星 2.*实现与验证/);
    expect(screen.getByRole('checkbox', { name: /候选行星 2/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /候选行星 3/ })).toHaveAccessibleName(/候选行星 3.*实现与验证/);
    expect(screen.getByText(/所有伙伴地位平等，可以直接互相 @、提问和回复/)).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /候选行星 3/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /候选行星 4/ })).toHaveAccessibleName(/候选行星 4.*实现与验证/);
    expect(screen.getByRole('combobox', { name: 'Room 边界配置模式' })).toHaveValue('full_trust');
    expect(screen.getByRole('combobox', { name: '行星 / Partner配置模式' })).toHaveValue('inherit');
    expect(screen.getByRole('combobox', { name: '卫星 / Tool Agent配置模式' })).toHaveValue('inherit');
    expect(screen.queryByRole('combobox', { name: '主持伙伴' })).not.toBeInTheDocument();
    expect(screen.queryByRole('group', { name: '发言方式' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '选择起始项目' }));
    expect(transport.filePickCalls).toEqual([{
      purpose: 'workspace-root',
      selection: 'directory',
      multiple: false,
      maxFiles: 1,
    }]);
    expect(screen.getAllByText('/Volumes/work/learnA').length).toBeGreaterThan(0);
  });

  it('creates a roleplay Room without forcing a workspace or coordinator mode', async () => {
    const created = {
      ...roomSummary('room-roleplay', '深夜茶话会'),
      roomKind: 'roleplay' as const,
      executionMode: 'per_action' as const,
      permissionPolicy: {
        schemaVersion: 'rag-ime.room-permission-policy.v1' as const,
        room: { executionMode: 'per_action' as const },
        partner: { executionMode: 'inherit' as const },
        toolAgent: { executionMode: 'inherit' as const },
      },
      avatar: 'sparkles',
      scenarioPrompt: '场景在安静的茶室。',
      routingPolicy: 'natural' as const,
      workspaceRoots: [],
    };
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.sessions.list': { ok: true, items: [] },
      'agent.rooms.create': { ok: true, room: created },
      'agent.room.snapshot': roomSnapshot('room-roleplay', [], '深夜茶话会'),
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('button', { name: '开始新的协作' }));
    await user.click(screen.getByRole('radio', { name: /一起聊聊/ }));
    expect(screen.queryByRole('button', { name: '选择工作目录' })).not.toBeInTheDocument();
    const roleplayRoomPermission = screen.getByRole('combobox', {
      name: 'Room 边界配置模式',
    });
    expect(roleplayRoomPermission).toHaveValue('per_action');
    expect(within(roleplayRoomPermission).queryByRole('option', {
      name: '全自动',
    })).not.toBeInTheDocument();
    const roleplayPartnerPermission = screen.getByRole('combobox', {
      name: '行星 / Partner配置模式',
    });
    expect(roleplayPartnerPermission).toHaveValue('inherit');
    expect(within(roleplayPartnerPermission).getByRole('option', {
      name: '全自动',
    })).toBeDisabled();
    expect(screen.getByRole('combobox', {
      name: '卫星 / Tool Agent配置模式',
    })).toHaveValue('inherit');
    await user.type(screen.getByRole('textbox', { name: '协作空间名称' }), '深夜茶话会');
    const optionalSummary = screen.getByText('补充背景与外观').closest('summary')!;
    const optionalDisclosure = optionalSummary.closest('details')!;
    expect(optionalDisclosure).toHaveClass('ui-disclosure');
    expect(optionalSummary).toHaveAttribute('aria-expanded', 'false');
    await user.click(optionalSummary);
    expect(optionalSummary).toHaveAttribute('aria-expanded', 'true');
    await user.type(screen.getByRole('textbox', { name: '共同背景' }), '场景在安静的茶室。');
    await user.click(optionalSummary);
    expect(optionalSummary).toHaveAttribute('aria-expanded', 'false');
    expect(optionalDisclosure.querySelector('.ui-disclosure__reveal')).toHaveAttribute('inert');
    await user.click(optionalSummary);
    expect(await screen.findByRole('textbox', { name: '共同背景' })).toHaveValue('场景在安静的茶室。');
    await user.click(screen.getByRole('button', { name: '开始群聊' }));

    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.rooms.create')).toBe(true));
    expect(transport.requests.find((call) => call.request.pathId === 'agent.rooms.create')?.request.body).toMatchObject({
      title: '深夜茶话会',
      roomKind: 'roleplay',
      avatar: 'sparkles',
      scenarioPrompt: '场景在安静的茶室。',
      routingPolicy: 'natural',
      workspaceRoots: [],
      permissionPolicy: {
        schemaVersion: 'rag-ime.room-permission-policy.v1',
        room: { executionMode: 'per_action' },
        partner: { executionMode: 'inherit' },
        toolAgent: { executionMode: 'inherit' },
      },
      routingConfig: { maxResponders: 1, naturalJitter: 0.04, fallbackParticipantId: '' },
    });
    const request = transport.requests.find(
      (call) => call.request.pathId === 'agent.rooms.create',
    )?.request;
    expect(request?.body).not.toHaveProperty('executionMode');
    expect(request?.body).not.toHaveProperty('dangerousModeConfirmation');
  });

  it('restores the collaboration permission default whenever the create dialog reopens', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.sessions.list': { ok: true, items: [] },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('button', { name: '开始新的协作' }));
    await user.click(screen.getByRole('radio', { name: /一起聊聊/ }));
    expect(screen.queryByRole('group', { name: '允许伙伴怎样工作' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '先不开始' }));

    await user.click(screen.getByRole('button', { name: '开始新的协作' }));
    expect(screen.getByRole('radio', { name: /一起完成任务/ })).toBeChecked();
    expect(screen.getByRole('combobox', { name: 'Room 边界配置模式' })).toHaveValue('full_trust');
    expect(screen.getByRole('combobox', { name: '行星 / Partner配置模式' })).toHaveValue('inherit');
    expect(screen.getByRole('combobox', { name: '卫星 / Tool Agent配置模式' })).toHaveValue('inherit');
  });

  it('sends an invite-only turn with a visible mention and structured participant id', async () => {
    const invitedRoom = {
      ...roomSummary('room-invite', '点名茶话会'),
      roomKind: 'roleplay' as const,
      routingPolicy: 'invite_only' as const,
      workspaceRoots: [],
    };
    const snapshot = roomSnapshot('room-invite', [], '点名茶话会');
    snapshot.room.roomKind = invitedRoom.roomKind;
    snapshot.room.routingPolicy = invitedRoom.routingPolicy;
    snapshot.room.workspaceRoots = invitedRoom.workspaceRoots;
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [invitedRoom] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': snapshot,
      'agent.room.message': { ok: true },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    await user.type(composer, '说说你的看法');
    expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: '添加内容' }));
    await user.click(screen.getByRole('menuitem', { name: /点名一位伙伴/ }));
    await user.click(screen.getByRole('option', { name: /Mars/ }));
    expect(composer).toHaveValue('说说你的看法 @Mars ');
    await user.click(screen.getByRole('button', { name: '发送消息' }));

    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.room.message')).toBe(true));
    expect(transport.requests.find((call) => call.request.pathId === 'agent.room.message')?.request.body).toMatchObject({
      message: '说说你的看法 @Mars',
      participantIds: ['room-invite:p2'],
    });
    expect(composer).toHaveValue('');
  });

  it('opens an avatar mention menu when typing at-sign and routes the chosen Agent', async () => {
    const invitedRoom = {
      ...roomSummary('room-mention', '点名协作'),
      routingPolicy: 'invite_only' as const,
    };
    const snapshot = roomSnapshot('room-mention', [], '点名协作');
    snapshot.room.routingPolicy = 'invite_only';
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [invitedRoom] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': snapshot,
      'agent.room.message': { ok: true },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    await user.type(composer, '@Mar');
    expect(await screen.findByRole('option', { name: /Mars/ })).toBeInTheDocument();
    await user.keyboard('{Enter}');
    expect(composer).toHaveValue('@Mars ');

    await user.type(composer, '核对记忆召回{Enter}');
    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.room.message')).toBe(true));
    expect(transport.requests.find((call) => call.request.pathId === 'agent.room.message')?.request.body).toMatchObject({
      message: '@Mars 核对记忆召回',
      participantIds: ['room-mention:p2'],
    });
  });

  it('keeps every explicit mention when dispatching a DuoAgent turn', async () => {
    const duoRoom = {
      ...roomSummary('room-duo', '双 Agent 核对'),
      routingPolicy: 'natural' as const,
    };
    const snapshot = roomSnapshot('room-duo', [], '双 Agent 核对');
    snapshot.room.routingPolicy = 'natural';
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [duoRoom] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': snapshot,
      'agent.room.message': { ok: true },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    await user.type(composer, '@Earth @Mars 分别检查实现和证据');
    await user.click(screen.getByRole('button', { name: '发送消息' }));

    await waitFor(() => expect(
      transport.requests.some((call) => call.request.pathId === 'agent.room.message'),
    ).toBe(true));
    expect(
      transport.requests.find((call) => call.request.pathId === 'agent.room.message')
        ?.request.body,
    ).toMatchObject({
      message: '@Earth @Mars 分别检查实现和证据',
      participantIds: ['room-duo:p1', 'room-duo:p2'],
    });
  });

  it('saves Room settings as future-turn configuration without changing its kind', async () => {
    const room = {
      ...roomSummary('room-settings', '旧名称'),
      roomKind: 'roleplay' as const,
      description: '旧简介',
      scenarioPrompt: '旧设定',
      routingPolicy: 'natural' as const,
      workspaceRoots: [],
      permissionPolicy: {
        schemaVersion: 'rag-ime.room-permission-policy.v1' as const,
        room: { executionMode: 'per_action' as const },
        partner: { executionMode: 'inherit' as const },
        toolAgent: { executionMode: 'inherit' as const },
      },
    };
    const updated = { ...room, title: '新名称', description: '新简介', scenarioPrompt: '新设定' };
    const snapshot = roomSnapshot(room.id, [], room.title);
    snapshot.room.roomKind = room.roomKind;
    snapshot.room.description = room.description;
    snapshot.room.scenarioPrompt = room.scenarioPrompt;
    snapshot.room.routingPolicy = room.routingPolicy;
    snapshot.room.workspaceRoots = room.workspaceRoots;
    snapshot.room.permissionPolicy = room.permissionPolicy;
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [room] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': snapshot,
      'agent.room.archive': { ok: true, room: updated },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('button', { name: '设置这个协作空间' }));
    expect(screen.getByRole('button', { name: '保存更改' })).toBeDisabled();
    await user.clear(screen.getByRole('textbox', { name: '协作空间名称' }));
    await user.type(screen.getByRole('textbox', { name: '协作空间名称' }), '新名称');
    await user.clear(screen.getByRole('textbox', { name: '协作空间简介' }));
    await user.type(screen.getByRole('textbox', { name: '协作空间简介' }), '新简介');
    const roleplayRoomPermission = screen.getByRole('combobox', {
      name: 'Room 边界配置模式',
    });
    expect(roleplayRoomPermission).toHaveValue('per_action');
    expect(within(roleplayRoomPermission).queryByRole('option', {
      name: '全自动',
    })).not.toBeInTheDocument();
    expect(screen.getByRole('combobox', {
      name: '行星 / Partner配置模式',
    })).toHaveValue('inherit');
    expect(screen.getByRole('combobox', {
      name: '卫星 / Tool Agent配置模式',
    })).toHaveValue('inherit');
    await user.clear(screen.getByRole('textbox', { name: '共同背景' }));
    await user.type(screen.getByRole('textbox', { name: '共同背景' }), '新设定');
    await user.click(screen.getByRole('button', { name: '保存更改' }));

    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.room.archive')).toBe(true));
    const request = transport.requests.find((call) => call.request.pathId === 'agent.room.archive')?.request;
    expect(request).toMatchObject({
      params: { roomId: room.id },
      body: {
        title: '新名称',
        description: '新简介',
        scenarioPrompt: '新设定',
        routingPolicy: room.routingPolicy,
        permissionPolicy: room.permissionPolicy,
      },
    });
    expect(request?.body).not.toHaveProperty('roomKind');
    expect(request?.body).not.toHaveProperty('executionMode');
    expect(request?.body).not.toHaveProperty('dangerousModeConfirmation');
  });

  it('loads and updates the stored three-layer Room policy without using the legacy projection', async () => {
    const room = roomSummary('room-permissions', '持续开发 Room');
    const fullTrustRoom = {
      ...room,
      executionMode: 'full_trust' as const,
      permissionPolicy: {
        schemaVersion: 'rag-ime.room-permission-policy.v1' as const,
        room: { executionMode: 'full_trust' as const },
        partner: { executionMode: 'inherit' as const },
        toolAgent: { executionMode: 'inherit' as const },
      },
    };
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [room] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': roomSnapshot(room.id, [], room.title),
      'agent.room.archive': (request: ControlRequest) => (
        request.body as { permissionPolicy?: { room?: { executionMode?: string } } } | undefined
      )?.permissionPolicy?.room?.executionMode === 'full_trust'
        ? { ok: true, room: fullTrustRoom }
        : { ok: true, room },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('button', { name: '设置这个协作空间' }));
    expect(screen.getByRole('combobox', { name: 'Room 边界配置模式' })).toHaveValue('workspace_managed');
    expect(screen.getByRole('combobox', { name: '行星 / Partner配置模式' })).toHaveValue('inherit');
    expect(screen.getByRole('combobox', { name: '卫星 / Tool Agent配置模式' })).toHaveValue('inherit');
    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Room 边界配置模式' }),
      'full_trust',
    );
    expect(screen.getAllByText('生效：全自动')).toHaveLength(3);
    await user.click(screen.getByRole('button', { name: '保存更改' }));

    await waitFor(() => expect(
      transport.requests.filter((call) => call.request.pathId === 'agent.room.archive'),
    ).toHaveLength(1));
    const permissionRequest = transport.requests
      .find((call) => call.request.pathId === 'agent.room.archive')
      ?.request;
    expect(permissionRequest).toMatchObject({
      params: { roomId: room.id },
      body: {
        title: room.title,
        avatar: 'briefcase',
        description: '',
        scenarioPrompt: '',
        routingPolicy: room.routingPolicy,
        permissionPolicy: {
          schemaVersion: 'rag-ime.room-permission-policy.v1',
          room: { executionMode: 'full_trust' },
          partner: { executionMode: 'inherit' },
          toolAgent: { executionMode: 'inherit' },
        },
        dangerousModeConfirmation: 'ENABLE_FULL_TRUST',
      },
    });
    expect(permissionRequest?.body).not.toHaveProperty('executionMode');
  });

  it('shows an unavailable migration state for an old Room without guessing a policy', async () => {
    const legacyRoom = roomSummary('room-legacy-policy', '旧 Room');
    delete legacyRoom.permissionPolicy;
    const snapshot = roomSnapshot(legacyRoom.id, [], legacyRoom.title);
    delete snapshot.room.permissionPolicy;
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [legacyRoom] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': snapshot,
      'agent.room.archive': { ok: true, room: { ...legacyRoom, title: '旧 Room 新名称' } },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('button', { name: '设置这个协作空间' }));
    expect(screen.getByLabelText('Room 分层权限不可用')).toHaveTextContent(
      '界面不会猜测或补成全权限',
    );
    await user.clear(screen.getByRole('textbox', { name: '协作空间名称' }));
    await user.type(screen.getByRole('textbox', { name: '协作空间名称' }), '旧 Room 新名称');
    await user.click(screen.getByRole('button', { name: '保存更改' }));

    await waitFor(() => expect(
      transport.requests.some((call) => call.request.pathId === 'agent.room.archive'),
    ).toBe(true));
    const update = transport.requests.find(
      (call) => call.request.pathId === 'agent.room.archive',
    )?.request;
    expect(update?.body).not.toHaveProperty('permissionPolicy');
    expect(update?.body).not.toHaveProperty('executionMode');
  });

  it('adds Venus to an existing Room and can remove the member again', async () => {
    const initial = roomSummary('room-members', '成员管理 Room');
    const futureParticipant = {
      id: 'room-members:p3', sessionId: 'room-members:s3', roleId: 'companion-future-v1', roleVersion: '1',
      displayName: '澄·远', collaborationRole: 'implementer' as const, status: 'active', ordinal: 2,
    };
    const withFuture = { ...initial, participants: [...initial.participants, futureParticipant] };
    const afterRemoval = {
      ...initial,
      participants: [...initial.participants, { ...futureParticipant, status: 'removed' }],
    };
    let addCompleted = false;
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [initial] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': roomSnapshot(initial.id, [], initial.title),
      'agent.room.participant.add': () => {
        addCompleted = true;
        return { ok: true, room: withFuture, participant: futureParticipant };
      },
      'agent.room.participant.remove': { ok: true, room: afterRemoval, participant: { ...futureParticipant, status: 'removed' } },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('button', { name: '设置这个协作空间' }));
    const invite = screen.getByRole('button', { name: '邀请 候选行星 1' });
    expect(invite).toBeEnabled();
    expect(screen.getByText(/不会补读此前的完整对话/)).toBeInTheDocument();
    await user.click(invite);

    await waitFor(() => expect(addCompleted).toBe(true));
    expect(transport.requests.find((call) => call.request.pathId === 'agent.room.participant.add')?.request).toMatchObject({
      params: { roomId: initial.id },
      body: { roleId: 'companion-future-v1', roleVersion: '1', collaborationRole: 'implementer' },
    });
    const remove = await screen.findByRole('button', { name: '移出 Venus' });
    expect(remove).toBeEnabled();
    await user.click(remove);
    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.room.participant.remove')).toBe(true));
    expect(transport.requests.find((call) => call.request.pathId === 'agent.room.participant.remove')?.request).toMatchObject({
      params: { roomId: initial.id },
      body: { participantId: 'room-members:p3' },
    });
  });

  it('changes a collaboration member job for future dispatches', async () => {
    const room = roomSummary('room-jobs', '岗位设置 Room');
    const target = room.participants[1];
    const updatedParticipant = { ...target, collaborationRole: 'reviewer' as const };
    const updated = {
      ...room,
      participants: room.participants.map((item) => item.id === target.id ? updatedParticipant : item),
    };
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [room] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': roomSnapshot(room.id, [], room.title),
      'agent.room.participant.update': { ok: true, room: updated, participant: updatedParticipant },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('button', { name: '设置这个协作空间' }));
    await user.click(screen.getByRole('combobox', { name: 'Mars 负责什么' }));
    await user.click(screen.getByRole('option', { name: '最终独立复核' }));

    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.room.participant.update')).toBe(true));
    expect(transport.requests.find((call) => call.request.pathId === 'agent.room.participant.update')?.request).toMatchObject({
      params: { roomId: room.id },
      body: { participantId: target.id, collaborationRole: 'reviewer' },
    });
    expect(screen.getByRole('combobox', { name: 'Mars 负责什么' })).toHaveTextContent('最终独立复核');
  });

  it('permanently deletes only an archived Room after exact-title confirmation', async () => {
    const room = { ...roomSummary('room-delete', '废弃协作 Room'), status: 'archived' };
    const snapshot = roomSnapshot(room.id, [], room.title);
    snapshot.room.status = 'archived';
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [room] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': snapshot,
      'agent.room.delete': { ok: true, roomId: room.id },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('button', { name: '设置这个协作空间' }));
    await user.click(screen.getByRole('button', { name: '删除协作空间' }));
    const confirm = screen.getByRole('textbox', { name: '输入协作空间名称确认永久删除' });
    const submit = screen.getByRole('button', { name: '永久删除' });
    expect(submit).toBeDisabled();
    await user.type(confirm, room.title);
    expect(submit).toBeEnabled();
    await user.click(submit);

    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.room.delete')).toBe(true));
    expect(transport.requests.find((call) => call.request.pathId === 'agent.room.delete')?.request).toMatchObject({
      params: { roomId: room.id },
      body: { confirmTitle: room.title },
    });
    expect(screen.queryByRole('button', { name: `打开协作空间：${room.title}` })).not.toBeInTheDocument();
  });

  it('renders the updated topic projection returned by the preview transport', async () => {
    const transport = createPreviewTransport();
    const user = userEvent.setup();
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider><RoomsFeature /></TooltipProvider>
      </ControlTransportProvider>,
    );

    await user.click(await screen.findByRole('button', { name: '管理话题' }));
    await user.type(screen.getByRole('textbox', { name: '话题名称' }), '发布风险');
    await user.type(screen.getByRole('textbox', { name: '话题摘要' }), '核对上线边界');
    await user.click(screen.getByRole('button', { name: '创建话题' }));

    await waitFor(() => {
      expect(within(screen.getByRole('dialog', { name: '整理话题' })).getByText('发布风险'))
        .toBeInTheDocument();
    });
    expect(screen.queryByText('服务端没有返回更新后的话题')).not.toBeInTheDocument();
  });

  it('creates an independent topic and adds a workspace-scoped shared artifact', async () => {
    const room = {
      ...roomSummary('room-context', '上下文 Room'),
      roomKind: 'collaboration' as const,
      activeTopicId: 'topic:default',
      topics: [
        { id: 'topic:default', roomId: 'room-context', title: '默认话题', summary: '', status: 'active' as const, ordinal: 0, createdAtMs: 1, updatedAtMs: 1 },
      ],
      artifacts: [],
    };
    const withTopic = {
      ...room,
      activeTopicId: 'topic:risk',
      topics: [
        ...room.topics,
        { id: 'topic:risk', roomId: room.id, title: '发布风险', summary: '核对上线边界', status: 'active' as const, ordinal: 1, createdAtMs: 2, updatedAtMs: 2 },
      ],
    };
    const withArtifact = {
      ...withTopic,
      artifacts: [
        { id: 'artifact:report', roomId: room.id, topicId: 'topic:risk', displayName: 'report.md', path: '/Volumes/work/learnA/report.md', mediaType: 'text/markdown', status: 'active' as const, createdAtMs: 3, updatedAtMs: 3 },
      ],
    };
    const snapshot = roomSnapshot(room.id, [], room.title);
    snapshot.room.roomKind = room.roomKind;
    snapshot.room.activeTopicId = room.activeTopicId;
    snapshot.room.topics = room.topics;
    snapshot.room.artifacts = room.artifacts;
    const transport = new MockControlTransport({
      pickedFiles: [{ id: 'report', name: 'report.md', mimeType: 'text/markdown', byteSize: 100, path: '/Volumes/work/learnA/report.md' }],
      routes: {
        'agent.rooms.list': { ok: true, items: [room] },
        'agent.roles.list': { ok: true, items: previewPersonas },
        'agent.room.snapshot': snapshot,
        'agent.room.topic.create': { ok: true, room: withTopic },
        'agent.room.artifact.add': { ok: true, room: withArtifact },
      },
    });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('button', { name: '管理话题' }));
    await user.type(screen.getByRole('textbox', { name: '话题名称' }), '发布风险');
    await user.type(screen.getByRole('textbox', { name: '话题摘要' }), '核对上线边界');
    await user.click(screen.getByRole('button', { name: '创建话题' }));
    await waitFor(() => expect(screen.getAllByText('发布风险').length).toBeGreaterThan(0));
    expect(transport.requests.find((call) => call.request.pathId === 'agent.room.topic.create')?.request.body).toEqual({
      title: '发布风险',
      summary: '核对上线边界',
    });
    const topicDialog = within(screen.getByRole('dialog', { name: '整理话题' }));
    await user.click(topicDialog.getAllByRole('button', { name: '关闭' }).find((button) => !button.hasAttribute('aria-label'))!);
    await user.click(screen.getByRole('button', { name: '分享工作文件' }));

    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.room.artifact.add')).toBe(true));
    expect(transport.filePickCalls.at(-1)).toEqual({
      purpose: 'room-artifact',
      selection: 'file',
      multiple: false,
      maxFiles: 1,
    });
    expect(transport.requests.find((call) => call.request.pathId === 'agent.room.artifact.add')?.request.body).toEqual({
      path: '/Volumes/work/learnA/report.md',
      displayName: 'report.md',
      mediaType: 'text/markdown',
      topicId: 'topic:risk',
    });
  });

  it('keeps a late shared-file failure inside the Room that started the picker', async () => {
    const pendingPick = deferred<Array<{ id: string; name: string; mimeType: string; byteSize: number; path: string }>>();
    const pendingAdd = deferred<{ ok: true }>();
    const transport = new MockControlTransport({
      pickedFiles: [{ id: 'capability-probe', name: 'probe.md', mimeType: 'text/markdown', byteSize: 1, path: '/Volumes/work/learnA/probe.md' }],
      routes: {
        'agent.rooms.list': { ok: true, items: [roomSummary('room-a', 'Room A'), roomSummary('room-b', 'Room B')] },
        'agent.roles.list': { ok: true, items: previewPersonas },
        'agent.room.snapshot': (request: ControlRequest) => roomSnapshot(String(request.params?.roomId ?? ''), []),
        'agent.room.artifact.add': () => pendingAdd.promise,
      },
    });
    vi.spyOn(transport, 'pickFiles').mockImplementation(() => pendingPick.promise);
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('button', { name: '分享工作文件' }));
    await user.click(screen.getByRole('button', { name: '打开协作空间：Room B' }));
    pendingPick.resolve([{
      id: 'late-report',
      name: 'late-report.md',
      mimeType: 'text/markdown',
      byteSize: 100,
      path: '/Volumes/work/learnA/late-report.md',
    }]);
    await waitFor(() => expect(transport.requests.some(({ request }) => (
      request.pathId === 'agent.room.artifact.add' && request.params?.roomId === 'room-a'
    ))).toBe(true));

    pendingAdd.reject(new Error('late Room A artifact failure'));
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: '打开协作空间：Room A' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('暂时无法分享这个文件');
    expect(transport.requests.find(({ request }) => request.pathId === 'agent.room.artifact.add')?.request.body).toMatchObject({
      path: '/Volumes/work/learnA/late-report.md',
    });
  });

  it('renders the Room list as soon as its catalog resolves', async () => {
    const pendingRoles = deferred<{ ok: true; items: typeof previewPersonas }>();
    const pendingSessions = deferred<{ ok: true; items: never[] }>();
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '立即可见 Room')] },
      'agent.roles.list': () => pendingRoles.promise,
      'agent.sessions.list': () => pendingSessions.promise,
      'agent.room.snapshot': roomSnapshot('room-a', []),
    } });

    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    expect(await screen.findByRole('button', { name: '更多协作空间操作' })).toBeInTheDocument();
    pendingRoles.resolve({ ok: true, items: previewPersonas });
    pendingSessions.resolve({ ok: true, items: [] });
  });

  it('archives a Room only after the real API confirms the state change', async () => {
    const archived = { ...roomSummary('room-a', '待收起协作空间'), status: 'archived' };
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '待收起协作空间')] },
      'agent.room.snapshot': roomSnapshot('room-a', []),
      'agent.room.archive': { ok: true, room: archived },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click((await screen.findAllByRole('button', { name: '更多协作空间操作' }))[0]!);
    await user.click(await screen.findByRole('menuitem', { name: '收起协作空间' }));
    await user.click(screen.getByRole('button', { name: '收起协作空间' }));

    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.room.archive')).toBe(true));
    const request = transport.requests.find((call) => call.request.pathId === 'agent.room.archive')?.request;
    expect(request).toMatchObject({ params: { roomId: 'room-a' }, body: { archived: true } });
    expect(await screen.findByText('选择一个协作空间')).toBeInTheDocument();
    expect(screen.getByText('选择一个协作空间').closest('.ui-empty-state')?.querySelector('img')).toBeNull();
    expect(screen.getByText('从左侧选择，或新建一个协作空间。')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '打开协作空间：待收起协作空间' })).not.toBeInTheDocument();
  });

  it('restores an archived Room directly through the real state transition', async () => {
    const archived = { ...roomSummary('room-a', '已收起协作空间'), status: 'archived' };
    const restored = { ...archived, status: 'active' };
    const snapshot = roomSnapshot('room-a', [], '已收起协作空间');
    snapshot.room.status = 'archived';
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': (request: ControlRequest) => ({
        ok: true,
        items: request.query?.includeArchived ? [archived] : [],
      }),
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': snapshot,
      'agent.room.archive': { ok: true, room: restored },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('button', { name: '显示已收起的协作空间' }));
    await user.click((await screen.findAllByRole('button', { name: '更多协作空间操作' }))[0]!);
    await user.click(await screen.findByRole('menuitem', { name: '恢复协作空间' }));
    expect(screen.queryByRole('heading', { name: '恢复这个协作空间？' })).not.toBeInTheDocument();

    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.room.archive')).toBe(true));
    expect(transport.requests.find((call) => call.request.pathId === 'agent.room.archive')?.request).toMatchObject({
      params: { roomId: 'room-a' },
      body: { archived: false },
    });
    expect(screen.getByRole('textbox', { name: '协作消息' })).toBeEnabled();
    await user.click(screen.getAllByRole('button', { name: '更多协作空间操作' })[0]!);
    expect(await screen.findByRole('menuitem', { name: '收起协作空间' })).toBeInTheDocument();
  });

  it('offers real participant addressing for manually routed Rooms', async () => {
    const manualRoom = { ...roomSummary('room-a', '点名协作'), routingPolicy: 'manual_mentions' };
    const snapshot = roomSnapshot('room-a', [], '点名协作');
    snapshot.room.routingPolicy = 'manual_mentions';
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [manualRoom] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': snapshot,
      'agent.room.message': { ok: true },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    await user.type(composer, '核对角色创建契约');
    expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: '添加内容' }));
    await user.click(screen.getByRole('menuitem', { name: /点名一位伙伴/ }));
    await user.click(screen.getByRole('option', { name: /Mars/ }));
    expect(composer).toHaveValue('核对角色创建契约 @Mars ');
    await user.click(screen.getByRole('button', { name: '发送消息' }));

    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.room.message')).toBe(true));
    expect(transport.requests.find((call) => call.request.pathId === 'agent.room.message')?.request.body).toMatchObject({
      message: '核对角色创建契约 @Mars',
      participantIds: ['room-a:p2'],
    });
  });

  it('disables the composer instead of exposing an inert send action without a Room', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [] },
      'agent.roles.list': { ok: true, items: [] },
    } });
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    expect(await screen.findByText('选择一个协作空间')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '协作消息' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled();
  });

  it('shows a useful empty conversation state after a real empty snapshot', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-empty', '空 Room')] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': roomSnapshot('room-empty', [], '空 Room'),
    } });
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    expect(await screen.findByText('还没有公开消息')).toBeInTheDocument();
    expect(screen.getByText('说出你想完成的事；只有遇到会影响实现的歧义，伙伴才会继续提问。')).toBeInTheDocument();
    expect(screen.getByText('还没有公开消息').closest('.ui-empty-state')?.querySelector('img')).toBeNull();
    expect(screen.getByRole('textbox', { name: '协作消息' })).toBeEnabled();
  });


  it('opens the shared status experience for the selected Room', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-status', '状态 Room')] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': roomSnapshot('room-status', [], '状态 Room'),
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await screen.findByText('还没有公开消息');
    await user.click(screen.getByRole('button', { name: '看看协作进展' }));
    expect(screen.getByRole('complementary', { name: '协作进展' })).toHaveAttribute('data-open', 'true');
    expect(screen.getByText('伙伴状态')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '收起进展面板' })).toBeInTheDocument();
  });



  it('keeps the real role catalog usable when the Room list request fails', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': () => { throw new Error('room catalog unavailable'); },
      'agent.roles.list': { ok: true, items: previewPersonas },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    expect(await screen.findByRole('alert')).toHaveTextContent('协作空间暂时无法读取，请稍后重试。');
    const create = screen.getByRole('button', { name: '开始新的协作' });
    expect(create).toBeEnabled();
    await user.click(create);
    expect(screen.getByRole('dialog', { name: '开始一起做事' })).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /候选行星 1/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /候选行星 2/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /候选行星 3/ })).toBeChecked();
  });

  it('keeps a Room creation failure inside the dialog and preserves the draft', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.sessions.list': { ok: true, items: [{ id: 'existing', mode: 'coordinator', workspaceRoots: ['/Volumes/work/learnA'] }] },
      'agent.rooms.create': () => { throw new Error('协作空间名称已存在'); },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    const create = await screen.findByRole('button', { name: '开始新的协作' });
    await waitFor(() => expect(create).toBeEnabled());
    await user.click(create);
    await user.type(screen.getByRole('textbox', { name: '协作空间名称' }), '发布前检查');
    await user.click(screen.getByRole('button', { name: '启用全自动并开始协作' }));

    const dialog = screen.getByRole('dialog', { name: '开始一起做事' });
    expect(await screen.findByRole('alert')).toHaveTextContent('协作空间名称已存在');
    expect(dialog).toContainElement(screen.getByRole('alert'));
    expect(screen.getByRole('textbox', { name: '协作空间名称' })).toHaveValue('发布前检查');
  });

  it('reloads a real Room snapshot after snapshot_required and resumes at the new cursor', async () => {
    let snapshotCalls = 0;
    const initial = roomSnapshot('room-a', [
      roomEvent('room-a', 1, 'user_message', { text: '初始消息' }),
    ]);
    const recovered = roomSnapshot('room-a', [
      roomEvent('room-a', 1, 'user_message', { text: '初始消息' }),
      roomEvent('room-a', 2, 'snapshot_required', { reason: 'room_event_replay_gap' }),
      roomEvent('room-a', 3, 'user_message', { text: '恢复后的消息' }),
    ]);
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '恢复协作空间')] },
      'agent.room.snapshot': () => (++snapshotCalls === 1 ? initial : recovered),
      'agent.room.message': { ok: true },
      'agent.rooms.create': { ok: true },
    } });
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);
    expect(await screen.findByText('初始消息')).toBeInTheDocument();

    transport.emit(
      'agent.room.events',
      roomEvent('room-a', 2, 'snapshot_required', { reason: 'room_event_replay_gap' }),
    );

    expect(await screen.findByText('恢复后的消息')).toBeInTheDocument();
    await waitFor(() => expect(snapshotCalls).toBe(2));
    expect(transport.subscriptionCalls.at(-1)?.request.lastEventId).toBe('room-a:3');
  });

  it('keeps the last good transcript while automatically restoring the live cursor', async () => {
    let snapshotCalls = 0;
    const initial = roomSnapshot('room-a', [
      roomEvent('room-a', 1, 'user_message', { text: '仍然保留的历史消息' }),
    ]);
    const recovered = roomSnapshot('room-a', [
      roomEvent('room-a', 1, 'user_message', { text: '仍然保留的历史消息' }),
      roomEvent('room-a', 2, 'snapshot_required', { reason: 'room_event_replay_gap' }),
      roomEvent('room-a', 3, 'user_message', { text: '重新同步后的消息' }),
    ]);
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '恢复失败协作空间')] },
      'agent.room.snapshot': () => {
        snapshotCalls += 1;
        if (snapshotCalls === 1) return initial;
        if (snapshotCalls === 2) throw new Error('snapshot recovery failed');
        return recovered;
      },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);
    expect(await screen.findByText('仍然保留的历史消息')).toBeInTheDocument();

    transport.emit(
      'agent.room.events',
      roomEvent('room-a', 2, 'snapshot_required', { reason: 'room_event_replay_gap' }),
    );

    expect(await screen.findByText(/正在恢复协作进度/)).toBeInTheDocument();
    expect(screen.getByText('仍然保留的历史消息')).toBeInTheDocument();
    expect(transport.activeSubscriptionCount()).toBe(0);
    await user.type(screen.getByRole('textbox', { name: '协作消息' }), '草稿不会隐藏同步错误');
    expect(screen.queryByRole('button', { name: '重试同步' })).not.toBeInTheDocument();

    expect(await screen.findByText('重新同步后的消息')).toBeInTheDocument();
    await waitFor(() => expect(snapshotCalls).toBe(3));
    expect(transport.subscriptionCalls.at(-1)?.request.lastEventId).toBe('room-a:3');
    expect(screen.queryByRole('button', { name: '重试同步' })).not.toBeInTheDocument();
    expect(screen.queryByText('正在恢复协作进度')).not.toBeInTheDocument();
    expect(transport.activeSubscriptionCount()).toBe(1);
  });

  it('stops live Room motion when only the conversation stream disconnects', async () => {
    const now = Date.now();
    const progress = {
      ...roomEvent('room-a', 1, 'participant_activity', {
        rootId: 'room-a:turn-1',
        dispatchId: 'room-a:dispatch-1',
        sourceEventType: 'current_progress',
        status: 'running',
        summary: '正在核对最新界面实现',
      }, {
        turnId: 'room-a:turn-1',
        participantId: 'room-a:p1',
        sourceSessionId: 'room-a:s1',
      }),
      createdAtMs: now,
    };
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '独立连接状态 Room')] },
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.snapshot': roomSnapshot('room-a', [progress]),
    } });
    const view = render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    expect((await screen.findAllByText('正在核对最新界面实现')).length).toBeGreaterThan(0);
    const lane = view.container.querySelector<HTMLElement>('.room-agent-lane')!;
    await waitFor(() => expect(lane).toHaveAttribute('data-motion', 'fresh'));

    act(() => {
      expect(transport.fail('agent.room.events', new Error('conversation stream interrupted'))).toBe(1);
    });

    await waitFor(() => expect(lane).toHaveAttribute('data-motion', 'disconnected'));
    expect(lane).toHaveTextContent('正在恢复 Room 对话实时更新');
    expect(lane.querySelector('.room-agent-lane__activity')).toHaveAttribute('data-motion', 'paused');
  });

  it('refreshes Room metadata without tearing down the live timeline subscription', async () => {
    let snapshotCalls = 0;
    let roomGetCalls = 0;
    const refreshed = roomSummary('room-a', '刷新后的 Room');
    refreshed.configRevision = 2;
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '原 Room')] },
      'agent.room.snapshot': () => {
        snapshotCalls += 1;
        return roomSnapshot('room-a', [
          roomEvent('room-a', 1, 'user_message', { text: '保持可见的消息' }),
        ]);
      },
      'agent.room.get': () => {
        roomGetCalls += 1;
        return { ok: true, room: refreshed };
      },
      'agent.room.message': { ok: true },
      'agent.rooms.create': { ok: true },
    } });
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);
    expect(await screen.findByText('保持可见的消息')).toBeInTheDocument();
    expect(transport.activeSubscriptionCount()).toBe(1);

    transport.emit(
      'agent.room.events',
      roomEvent('room-a', 2, 'room_config_changed', { configRevision: 2 }),
    );
    transport.emit(
      'agent.room.events',
      roomEvent('room-a', 3, 'participant_activity', {
        activityKind: 'work',
        summary: '工作状态已更新',
      }),
    );

    await waitFor(() => expect(roomGetCalls).toBe(1));
    expect(snapshotCalls).toBe(1);
    expect(transport.subscriptionCalls).toHaveLength(1);
    expect(transport.activeSubscriptionCount()).toBe(1);
    expect(screen.getAllByText('刷新后的 Room').length).toBeGreaterThan(0);
    expect(screen.getByText('保持可见的消息')).toBeInTheDocument();
  });

  it('cancels a late snapshot when the user selects another Room', async () => {
    const first = deferred<ReturnType<typeof roomSnapshot>>();
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': {
        ok: true,
        items: [roomSummary('room-a', 'Room A'), roomSummary('room-b', 'Room B')],
      },
      'agent.room.snapshot': (request: ControlRequest) => request.params?.roomId === 'room-a'
        ? first.promise
        : roomSnapshot('room-b', [roomEvent('room-b', 1, 'user_message', { text: 'Room B 历史' })]),
      'agent.room.message': { ok: true },
      'agent.rooms.create': { ok: true },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);
    await user.click(await screen.findByRole('button', { name: /Room B/ }));
    expect(await screen.findByText('Room B 历史')).toBeInTheDocument();

    first.resolve(roomSnapshot('room-a', [
      roomEvent('room-a', 1, 'user_message', { text: '迟到的 Room A 历史' }),
    ]));

    await Promise.resolve();
    await Promise.resolve();
    expect(screen.queryByText('迟到的 Room A 历史')).not.toBeInTheDocument();
    expect(screen.getByText('Room B 历史')).toBeInTheDocument();
    expect(transport.activeSubscriptionCount()).toBe(1);
  });

  it('shows the complete Room working boundary without per-role permission switches', async () => {
    const room = roomSummary('room-a', '权限 Room');
    const tools = [
      { id: 'overview', displayName: '控制中心概览', description: '查看整体状态', sessionModes: ['assistant', 'coordinator'], operations: ['status'], profileOperations: { 'control-center-v1': ['status'], 'subagent-readonly-v1': ['status'] }, enabled: true },
      { id: 'memory', displayName: '记忆与工具书', description: '检索记忆', sessionModes: ['assistant', 'coordinator'], operations: ['catalog'], profileOperations: { 'control-center-v1': ['catalog'], 'subagent-readonly-v1': ['catalog'] }, enabled: true },
      { id: 'workspace_lsp', displayName: '代码智能', description: '读取代码语义并受控应用重命名', sessionModes: ['coordinator'], operations: ['symbols'], profileOperations: { 'control-center-v1': ['symbols'], 'subagent-readonly-v1': ['symbols'] }, enabled: true },
    ];
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [room] },
      'agent.room.snapshot': roomSnapshot('room-a', []),
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.tools.list': { ok: true, items: tools },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('radio', { name: '伙伴' }));
    expect(screen.getByRole('region', { name: '伙伴与权限' })).toHaveTextContent('伙伴与工作权限');
    const sessionListRequestsBeforeBoundary = transport.requests.filter(
      (call) => call.request.pathId === 'agent.sessions.list',
    ).length;
    await user.click((await screen.findAllByRole('button', { name: '查看能做什么' }))[0]!);
    expect(await screen.findByRole('dialog', { name: /Earth\s+能做什么/ })).toBeInTheDocument();
    expect(transport.requests.filter(
      (call) => call.request.pathId === 'agent.sessions.list',
    )).toHaveLength(sessionListRequestsBeforeBoundary);
    expect(transport.requests.find((call) => call.request.pathId === 'agent.tools.list')?.request.query).toEqual({ sessionId: 'room-a:s1' });
    expect(screen.getByText('行星 / Partner')).toBeInTheDocument();
    expect(screen.getByText('卫星 / Tool Agent')).toBeInTheDocument();
    expect(screen.getAllByText('配置：继承（Inherit）')).toHaveLength(2);
    expect(screen.getAllByText('生效：工作区托管（沙箱）')).toHaveLength(3);
    expect(screen.getAllByText('/Volumes/work/learnA').length).toBeGreaterThan(0);
    expect(screen.queryByRole('radio', { name: '只读' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '保存权限' })).not.toBeInTheDocument();
    expect(screen.queryByText(/私有 Session：/)).not.toBeInTheDocument();
    const toolsSummary = screen.getByText(/看看 Session 暴露了哪些工具/).closest('summary')!;
    const toolsDisclosure = toolsSummary.closest('details')!;
    expect(toolsDisclosure).toHaveClass('ui-disclosure');
    expect(toolsSummary).toHaveAttribute('aria-expanded', 'false');
    expect(toolsDisclosure.querySelector('.ui-disclosure__reveal')).toHaveAttribute('inert');
    await user.click(toolsSummary);
    expect(toolsSummary).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('Session 基础工具')).toBeInTheDocument();
    expect(screen.getByText('读取文件')).toBeInTheDocument();
    expect(screen.getByText('编辑文件')).toBeInTheDocument();
    expect(screen.getByText('写入文件')).toBeInTheDocument();
    expect(screen.getByText('运行命令')).toBeInTheDocument();
    expect(screen.getByText('代码智能')).toBeInTheDocument();
    expect(screen.queryByText('工作区读取')).not.toBeInTheDocument();
    expect(screen.queryByText('受控命令')).not.toBeInTheDocument();
    expect(transport.requests.some((call) => call.request.pathId === 'agent.session.mode.update')).toBe(false);
    await user.click(toolsSummary);
    expect(toolsSummary).toHaveAttribute('aria-expanded', 'false');
    expect(toolsDisclosure.querySelector('.ui-disclosure__reveal')).toHaveAttribute('inert');
  });

  it('does not turn a stale approval message block into a dead review link', () => {
    const room = roomSummary('room-a', '审阅 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-a');
    projection.messageOrder.push('assistant');
    projection.turnsById['turn-a'] = {
      id: 'turn-a', status: 'running', messageIds: ['assistant'], activityIds: [],
      participantIds: ['room-a:p1'], createdAtMs: 1, updatedAtMs: 1,
    };
    projection.messagesById.assistant = {
      id: 'assistant', roomId: room.id, turnId: 'turn-a', participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1', role: 'assistant', status: 'completed', text: '', createdAtMs: 1,
      message: {
        schemaVersion: 'rag-ime.agent-message.v1', id: 'assistant', sessionId: 'room-a:s1',
        turnId: 'turn-a', role: 'assistant', status: 'completed', attachments: [], citations: [], createdAtMs: 1,
        blocks: [{ id: 'approval', type: 'approval', status: 'running', presentationKind: 'approval', data: { approvalId: 'approval:1', payloadSha256: 'a'.repeat(64), state: 'pending', title: '应用设置' } }],
      },
    };

    render(<RoomTurn turnId="turn-a" room={room} projection={projection} personas={previewPersonas} />);
    expect(screen.queryByRole('link', { name: /立即审阅/ })).not.toBeInTheDocument();
  });

  it('handles a real participant approval inside the Room', async () => {
    const room = roomSummary('room-a', '内联审批 Room');
    const projection = createRoomProjection(room.id);
    const decide = vi.fn(async () => undefined);
    projection.turnOrder.push('turn-a');
    projection.activityOrder.push('approval:1');
    projection.turnsById['turn-a'] = {
      id: 'turn-a', status: 'running', messageIds: [], activityIds: ['approval:1'],
      participantIds: ['room-a:p1'], createdAtMs: 1, updatedAtMs: 1,
    };
    projection.activitiesById['approval:1'] = {
      id: 'approval:1', turnId: 'turn-a', participantId: 'room-a:p1', sourceSessionId: 'room-a:s1',
      kind: 'participant_activity', status: 'waiting', summary: '等待确认写入文件',
      payload: {
        sourceEventType: 'approval_required',
        approvalId: 'approval:1',
        payloadSha256: 'a'.repeat(64),
        toolId: 'workspace_write',
        operation: 'apply',
        state: 'pending',
      },
      createdAtMs: 1,
    };

    render(<RoomTurn
      turnId="turn-a"
      room={room}
      projection={projection}
      personas={previewPersonas}
      onApprovalDecision={decide}
    />);
    const approvalRegion = screen.getByRole('region', { name: 'Room 审批' });
    expect(approvalRegion.closest('details.room-agent-lane')).toHaveAttribute('open');
    expect(approvalRegion.closest('details.room-agent-lane__activity')).toHaveAttribute('open');
    expect(screen.getByText('等待审批')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '批准并继续' }));

    await waitFor(() => expect(decide).toHaveBeenCalledWith(
      'approval:1',
      'approved',
      'a'.repeat(64),
    ));
    expect(screen.queryByRole('link', { name: /立即审阅/ })).not.toBeInTheDocument();
  });

  it('links a pending plan review to the authoritative participant Session', () => {
    const room = roomSummary('room-a', '计划审阅 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-a');
    projection.activityOrder.push('review:1');
    projection.turnsById['turn-a'] = {
      id: 'turn-a', status: 'running', messageIds: [], activityIds: ['review:1'],
      participantIds: ['room-a:p1'], createdAtMs: 1, updatedAtMs: 1,
    };
    projection.activitiesById['review:1'] = {
      id: 'review:1', turnId: 'turn-a', participantId: 'room-a:p1', sourceSessionId: 'room-a:s1',
      kind: 'participant_activity', status: 'waiting', summary: '请选择是否批准计划',
      payload: { requestKind: 'plan_review', requestId: 'plan:review:1' }, createdAtMs: 1,
    };

    render(<RoomTurn turnId="turn-a" room={room} projection={projection} personas={previewPersonas} />);
    expect(screen.getByText('等待审阅')).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: /立即审阅/ })).toHaveLength(1);
    expect(screen.getByRole('link', { name: /立即审阅/ })).toHaveAttribute('href', '#/agent?session=room-a%3As1');
    expect(screen.queryByRole('button', { name: /停止/ })).not.toBeInTheDocument();
  });

  it('opens a generic selectable clarification in the owning Session', () => {
    const room = roomSummary('room-a', '选择 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-a');
    projection.turnsById['turn-a'] = {
      id: 'turn-a', status: 'running', messageIds: [], activityIds: ['select:1'],
      participantIds: ['room-a:p1'], createdAtMs: 1, updatedAtMs: 1,
    };
    projection.activitiesById['select:1'] = {
      id: 'select:1', turnId: 'turn-a', participantId: 'room-a:p1', sourceSessionId: 'room-a:s1',
      kind: 'participant_activity', status: 'waiting', summary: '选择部署环境',
      payload: {
        requestKind: 'user_input_required',
        requestId: 'select:1',
        method: 'select',
        options: [{ id: 'staging', label: '预发布' }, { id: 'production', label: '生产' }],
      },
      createdAtMs: 1,
    };

    render(<RoomTurn turnId="turn-a" room={room} projection={projection} personas={previewPersonas} />);
    expect(screen.getByText('等待选择')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /立即选择/ })).toHaveAttribute('href', '#/agent?session=room-a%3As1');
  });

  it('routes grouped clarification to the owning participant without merging answers into the Room timeline', () => {
    const room = roomSummary('room-a', '协作确认');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-a');
    projection.turnsById['turn-a'] = {
      id: 'turn-a', status: 'running', messageIds: [], activityIds: ['grouped:1'],
      participantIds: ['room-a:p1'], createdAtMs: 1, updatedAtMs: 1,
    };
    projection.activitiesById['grouped:1'] = {
      id: 'grouped:1', turnId: 'turn-a', participantId: 'room-a:p1', sourceSessionId: 'room-a:s1',
      kind: 'participant_activity', status: 'waiting', summary: '一起确认交付方式',
      payload: {
        requestKind: 'grouped_questions',
        requestId: 'grouped:1',
        questions: [
          { id: 'scope', question: '先覆盖哪一部分？', options: ['核心流程', '完整流程'] },
          { id: 'review', question: '如何复核？', options: ['伙伴互查', '直接交付'] },
        ],
      },
      createdAtMs: 1,
    };

    render(<RoomTurn turnId="turn-a" room={room} projection={projection} personas={previewPersonas} />);
    expect(screen.getByText('等待回答')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /立即回答/ })).toHaveAttribute('href', '#/agent?session=room-a%3As1');
    expect(screen.queryByText('核心流程')).not.toBeInTheDocument();
    expect(screen.queryByText('伙伴互查')).not.toBeInTheDocument();
  });

  it('keeps private activity rows out of the public Post timeline', () => {
    const room: RoomSummary = {
      id: 'room-a', title: '迁移作战室', status: 'active', routingPolicy: 'moderator',
      moderatorParticipantId: 'p1', updatedAtMs: Date.now(),
      participants: [
        { id: 'p1', sessionId: 's1', roleId: 'companion-present-v1', roleVersion: '1', displayName: '澄', status: 'active', ordinal: 0 },
        { id: 'p2', sessionId: 's2', roleId: 'companion-firstlight-v1', roleVersion: '1', displayName: '澄·初', status: 'active', ordinal: 1 },
      ],
    };
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-a');
    projection.activityOrder.push('activity-a');
    projection.turnsById['turn-a'] = {
      id: 'turn-a', status: 'running', messageIds: [], activityIds: ['activity-a'],
      participantIds: ['p1'], createdAtMs: 1, updatedAtMs: 1,
    };
    projection.activitiesById['activity-a'] = {
      id: 'activity-a', turnId: 'turn-a', participantId: 'p1', sourceSessionId: 's1',
      kind: 'participant_activity', status: 'running', summary: '核对移动端布局', payload: {}, createdAtMs: 1,
    };
    render(<RoomTurn turnId="turn-a" room={room} projection={projection} personas={previewPersonas} />);
    expect(document.querySelector('.room-group-activity')).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent('核对移动端布局');
  });

  it('shows a bounded tool lifecycle with one root-owned stop action', async () => {
    const room = roomSummary('room-a', '实时执行 Room');
    const projection = createRoomProjection(room.id);
    const now = Date.now();
    projection.turnOrder.push('turn-a');
    projection.activityOrder.push('tool-a');
    projection.turnsById['turn-a'] = {
      id: 'turn-a', rootId: 'room-turn:root-a', status: 'running', messageIds: [], activityIds: ['tool-a'],
      participantIds: ['room-a:p1'], createdAtMs: now - 1_200, updatedAtMs: now,
    };
    projection.activitiesById['tool-a'] = {
      id: 'tool-a', turnId: 'turn-a', participantId: 'room-a:p1', sourceSessionId: 'room-a:s1',
      kind: 'participant_activity', status: 'running', summary: 'grep',
      payload: {
        sourceEventType: 'tool_started',
        toolName: 'grep',
        toolCallId: 'call-a',
        arguments: {
          path: '…/project/rag_ime',
          pattern: 'room_event_projection',
          limit: 40,
        },
      },
      createdAtMs: now - 1_000,
    };
    const onAbortTurn = vi.fn();
    const user = userEvent.setup();

    render(<RoomTurn
      turnId="turn-a"
      room={room}
      projection={projection}
      personas={previewPersonas}
      onAbortTurn={onAbortTurn}
    />);

    const lane = document.querySelector<HTMLDetailsElement>('.room-agent-lane')!;
    await user.click(lane.querySelector(':scope > summary')!);
    const toolTitle = screen.getByText('在 …/project/rag_ime 搜索 “协作记录”');
    expect(toolTitle).toBeInTheDocument();
    const toolSummary = toolTitle.closest('summary')!;
    expect(toolSummary).toHaveTextContent('运行记录 · 搜索文本 · 进行中');
    expect(toolSummary).toHaveAttribute('aria-expanded', 'true');
    await user.click(toolSummary);
    expect(toolSummary).toHaveAttribute('aria-expanded', 'false');
    await user.click(toolSummary);
    expect(toolSummary).toHaveAttribute('aria-expanded', 'true');
    expect(document.activeElement).toBe(toolSummary);
    expect(screen.getByLabelText('工具调用参数')).toHaveTextContent('协作记录');
    expect(screen.getByLabelText('工具调用参数')).not.toHaveTextContent('room_event_projection');
    // Module import and user-event setup can cross a second boundary on a
    // loaded machine; assert the real elapsed-time shape instead of freezing
    // the wall clock to exactly one second.
    expect(screen.getByText(/[1-9]\d*s/)).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: '停止本轮任务' })).toHaveLength(1);
    await user.click(screen.getByRole('button', { name: '停止本轮任务' }));
    expect(onAbortTurn).toHaveBeenCalledWith('room-turn:root-a');
  });



  it('shows replayed read arguments and bounded result behind a copyable row disclosure', async () => {
    const room = roomSummary('room-a', '工具回执 Room');
    const projection = createRoomProjection(room.id);
    const now = Date.now();
    projection.turnOrder.push('turn-a');
    projection.activityOrder.push('read-a');
    projection.turnsById['turn-a'] = {
      id: 'turn-a', status: 'running', messageIds: [], activityIds: ['read-a'],
      participantIds: ['room-a:p1'], createdAtMs: now - 1_200, updatedAtMs: now,
    };
    projection.activitiesById['read-a'] = {
      id: 'read-a', turnId: 'turn-a', participantId: 'room-a:p1', sourceSessionId: 'room-a:s1',
      kind: 'participant_activity', status: 'completed', summary: 'read',
      payload: {
        sourceEventType: 'tool_finished',
        toolName: 'read',
        toolCallId: 'call-read',
        arguments: {
          path: '…/project/src/runtime.ts',
          offset: 80,
          limit: 24,
        },
        result: {
          path: '…/project/src/runtime.ts',
          offset: 80,
          limit: 24,
          outputPreview: '80:export function settleRoom() {\\n81:  return receipt;\\n82:}',
          outputTruncated: false,
        },
      },
      createdAtMs: now - 1_000,
      updatedAtMs: now - 700,
    };
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const { container } = render(
      <RoomTurn turnId="turn-a" room={room} projection={projection} personas={previewPersonas} />,
    );

    const row = container.querySelector<HTMLDetailsElement>('.room-agent-activity--tool')!;
    expect(row).not.toHaveAttribute('open');
    expect(row).toHaveTextContent('…/project/src/runtime.ts 已读取');
    expect(row).not.toHaveTextContent('settleRoom');
    await user.click(container.querySelector<HTMLDetailsElement>('.room-agent-lane')!.querySelector(':scope > summary')!);
    await user.click(row.querySelector('summary')!);

    expect(within(row).getByLabelText('工具调用参数')).toHaveTextContent('80');
    expect(within(row).getByLabelText('工具调用参数')).toHaveTextContent('24');
    expect(within(row).getByLabelText('工具返回内容')).toHaveTextContent('settleRoom');
    await user.click(within(row).getByRole('button', { name: '复制结果' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(
      '80:export function settleRoom() {\\n81:  return receipt;\\n82:}',
    ));
  });

  it('keeps humane Room receipt semantics while hiding protocol identifiers', async () => {
    const room = roomSummary('room-a', 'Room 状态回执');
    const identity = {
      turnId: 'room-a:turn-1',
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
    };
    const started = reduceRoomEvent(
      createRoomProjection(room.id),
      parseRoomEvent(roomEvent('room-a', 1, 'participant_activity', {
        rootId: 'room-a:turn-1',
        dispatchId: 'dispatch-a',
        sourceEventType: 'tool_started',
        toolName: 'room_state',
        toolCallId: 'call-control',
        summary: '准备查看协作状态',
        status: 'running',
        arguments: {
          operation: 'status',
          includeEvidence: true,
          intent: '请伙伴复核状态',
          objective: '确认 Room 是否同步',
          expectedOutput: '一条公开状态回执',
          acceptance: '状态版本可核对',
          kind: 'status_check',
          mentions: ['room-a:p2'],
          waitingFor: 'room-a:p2',
          blocker: '无',
        },
      }, identity)),
    ).state;
    const projection = reduceRoomEvent(
      started,
      parseRoomEvent(roomEvent('room-a', 2, 'participant_activity', {
        rootId: 'room-a:turn-1',
        dispatchId: 'dispatch-a',
        sourceEventType: 'tool_finished',
        toolName: 'room_state',
        toolCallId: 'call-control',
        result: {
          unchanged: true,
          ok: true,
          created: true,
          executionPerformed: true,
          stateRevision: 12,
          evidenceRef: {
            id: 'safe-room-receipt-42',
            revision: 3,
          },
          currentResponsibility: {
            state: 'running',
            objective: '继续核对 Room 状态',
          },
          status: 'ready',
        },
      }, identity)),
    ).state;
    const user = userEvent.setup();
    const { container } = render(
      <RoomTurn turnId="room-a:turn-1" room={room} projection={projection} personas={previewPersonas} />,
    );
    const row = container.querySelector<HTMLDetailsElement>('.room-agent-activity--tool')!;

    expect(row).toHaveAttribute('data-state', 'completed');
    expect(row).toHaveTextContent('查看协作状态 已完成');
    expect(row).not.toHaveTextContent('准备查看协作状态');
    await user.click(row.querySelector('summary')!);
    const request = within(row).getByLabelText('工具调用参数');
    expect(request).toHaveTextContent('协作意图');
    expect(request).toHaveTextContent('任务目标');
    expect(request).toHaveTextContent('预期交付');
    expect(request).toHaveTextContent('操作');
    expect(request).toHaveTextContent('等待对象');
    expect(request).not.toHaveTextContent('room-a:p2');
    expect(request).not.toHaveTextContent('expectedOutput');
    expect(request).not.toHaveTextContent('waitingFor');
    const returned = within(row).getByLabelText('工具结果明细');
    expect(returned).toHaveTextContent('成功');
    expect(returned).toHaveTextContent('可用');
    expect(returned).toHaveTextContent('无变更');
    expect(returned).toHaveTextContent('状态版本');
    expect(returned).toHaveTextContent('第 12 版');
    expect(returned).toHaveTextContent('验证依据已保留');
    expect(returned).toHaveTextContent('当前职责');
    expect(returned).toHaveTextContent('状态：进行中');
    expect(returned).not.toHaveTextContent('safe-room-receipt-42');
    expect(returned).not.toHaveTextContent('executionPerformed');
    expect(returned).not.toHaveTextContent('stateRevision');
    expect(returned).not.toHaveTextContent('evidenceRef');
  });

  it('keeps governed work lanes folded by default and resets disclosure across Root and Room scope', async () => {
    const room = roomSummary('room-a', '公开进度 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-progress');
    projection.turnsById['turn-progress'] = {
      id: 'turn-progress',
      rootId: 'turn-progress',
      status: 'running',
      messageIds: [],
      activityIds: ['reasoning-a', 'read-a', 'progress-a'],
      participantIds: ['room-a:p1'],
      dispatchIds: ['dispatch-progress'],
      dispatchParticipantIds: { 'dispatch-progress': 'room-a:p1' },
      createdAtMs: 1,
      updatedAtMs: 4,
    };
    const base = {
      turnId: 'turn-progress',
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
      kind: 'participant_activity' as const,
      createdAtMs: 1,
    };
    projection.activitiesById['reasoning-a'] = {
      ...base,
      id: 'reasoning-a',
      status: 'running',
      summary: '先确认调用链，再修改入口',
      payload: {
        rootId: 'turn-progress',
        dispatchId: 'dispatch-progress',
        sourceEventType: 'reasoning_summary',
        source: 'provider_reasoning_summary',
      },
    };
    projection.activitiesById['read-a'] = {
      ...base,
      id: 'read-a',
      status: 'completed',
      summary: '读取 src/runtime.ts',
      payload: {
        rootId: 'turn-progress',
        dispatchId: 'dispatch-progress',
        sourceEventType: 'tool_finished',
        toolName: 'read',
        toolCallId: 'read-call',
        arguments: { path: '…/project/src/runtime.ts' },
      },
      createdAtMs: 2,
    };
    projection.activitiesById['progress-a'] = {
      ...base,
      id: 'progress-a',
      status: 'running',
      summary: '入口已修改，正在核对调用方',
      payload: {
        rootId: 'turn-progress',
        dispatchId: 'dispatch-progress',
        sourceEventType: 'current_progress',
      },
      createdAtMs: 3,
    };

    const user = userEvent.setup();
    const view = render(
      <RoomTurn turnId="turn-progress" room={room} projection={projection} personas={previewPersonas} />,
    );
    const { container } = view;
    const lane = container.querySelector<HTMLDetailsElement>('.room-agent-lane')!;
    const work = container.querySelector('.room-agent-lane__work')!;
    const rows = [...container.querySelectorAll('.room-agent-activity')];

    expect(lane).not.toHaveAttribute('open');
    expect(work).toHaveTextContent('入口已修改，正在核对调用方');
    expect(work).toHaveTextContent('3 条运行记录');
    expect(work).toHaveTextContent('Earth');
    await user.click(lane.querySelector('summary')!);
    expect(lane).toHaveAttribute('open');
    expect(rows).toHaveLength(3);
    expect(work).toHaveTextContent('工作摘要');
    expect(rows[0]).toHaveTextContent('先确认调用链，再修改入口');
    expect(rows[0]).toHaveTextContent('最新思考摘要');
    expect(rows[0]).not.toHaveTextContent('伙伴自述');
    expect(rows[1]).toHaveTextContent('运行记录');
    expect(rows[2]).toHaveTextContent('进度更新');
    expect(rows[1]).toHaveTextContent('runtime.ts');
    expect(rows[2]).toHaveTextContent('入口已修改，正在核对调用方');

    projection.turnOrder.push('turn-next');
    projection.activityOrder.push('progress-next');
    projection.turnsById['turn-next'] = {
      ...projection.turnsById['turn-progress']!,
      id: 'turn-next',
      rootId: 'root-next',
      activityIds: ['progress-next'],
      dispatchIds: ['dispatch-next'],
      dispatchParticipantIds: { 'dispatch-next': 'room-a:p1' },
    };
    projection.activitiesById['progress-next'] = {
      ...projection.activitiesById['progress-a']!,
      id: 'progress-next',
      turnId: 'turn-next',
      payload: {
        ...projection.activitiesById['progress-a']!.payload,
        rootId: 'root-next',
        dispatchId: 'dispatch-next',
      },
    };
    view.rerender(
      <RoomTurn turnId="turn-next" room={room} projection={projection} personas={previewPersonas} />,
    );
    const nextLane = container.querySelector<HTMLDetailsElement>('.room-agent-lane')!;
    expect(nextLane).not.toHaveAttribute('open');
    await user.click(nextLane.querySelector('summary')!);
    expect(nextLane).toHaveAttribute('open');

    view.rerender(
      <RoomTurn
        turnId="turn-next"
        room={room}
        projection={{ ...projection, roomId: 'room-b' }}
        personas={previewPersonas}
      />,
    );
    const roomScopedLane = container.querySelector<HTMLDetailsElement>('.room-agent-lane')!;
    expect(roomScopedLane.querySelector(':scope > summary')).toHaveAttribute('aria-expanded', 'false');
    expect(roomScopedLane).toHaveAttribute('open');
    await waitFor(() => expect(roomScopedLane).not.toHaveAttribute('open'));
  });

  it('renders two separate same-name tool calls as two cards', () => {
    const room = roomSummary('room-a', '工具调用 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-a');
    projection.turnsById['turn-a'] = {
      id: 'turn-a', status: 'running', messageIds: [], activityIds: ['read-a', 'read-b', 'todo-a'],
      participantIds: ['room-a:p1'], createdAtMs: 1, updatedAtMs: 4,
    };
    for (const [index, id] of ['read-a', 'read-b'].entries()) {
      projection.activitiesById[id] = {
        id,
        turnId: 'turn-a',
        participantId: 'room-a:p1',
        sourceSessionId: 'room-a:s1',
        kind: 'participant_activity',
        status: 'completed',
        summary: `读取第 ${index + 1} 个文件`,
        payload: {
          sourceEventType: 'tool_finished',
          toolName: 'read',
          toolCallId: `call-${index + 1}`,
          arguments: { path: `…/project/src/file-${index + 1}.ts` },
          result: { outputPreview: `file ${index + 1}`, outputTruncated: false },
        },
        createdAtMs: index + 1,
      };
    }
    projection.activitiesById['todo-a'] = {
      id: 'todo-a',
      turnId: 'turn-a',
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
      kind: 'participant_activity',
      status: 'completed',
      summary: 'Todo：6/6 已完成，0 进行中，0 待处理',
      payload: {
        sourceEventType: 'tool_finished',
        toolName: 'todo',
        toolCallId: 'call-todo',
        result: { outputPreview: 'Todo：6/6 已完成' },
      },
      createdAtMs: 3,
    };
    const { container } = render(
      <RoomTurn turnId="turn-a" room={room} projection={projection} personas={previewPersonas} />,
    );
    const cards = container.querySelectorAll<HTMLElement>('.room-agent-activity--tool');

    expect(cards).toHaveLength(2);
    expect(cards[0]).toHaveTextContent('file-1.ts');
    expect(cards[1]).toHaveTextContent('file-2.ts');
    expect(container).not.toHaveTextContent('Todo：6/6 已完成');
    expect(container.querySelector('.room-agent-activity--tool-group')).not.toBeInTheDocument();
    expect(container.querySelector('.room-agent-lane__activity-feed')).toContainElement(cards[0]);
    expect(container.querySelector('.room-agent-lane__activity-feed')).toContainElement(cards[1]);
  });

  it('separates returned steps from a stopped Root and exposes the concrete step list', async () => {
    const room = roomSummary('room-a', '停止后核对 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-stopped');
    projection.turnsById['turn-stopped'] = {
      id: 'turn-stopped',
      rootId: 'turn-stopped',
      status: 'aborted',
      messageIds: [],
      activityIds: ['read-finished', 'write-finished'],
      participantIds: ['room-a:p1'],
      dispatchIds: ['dispatch-stopped'],
      terminalParticipantIds: ['room-a:p1'],
      failedParticipantIds: [],
      abortedParticipantIds: ['room-a:p1'],
      terminalDispatchIds: ['dispatch-stopped'],
      failedDispatchIds: [],
      abortedDispatchIds: ['dispatch-stopped'],
      dispatchParticipantIds: { 'dispatch-stopped': 'room-a:p1' },
      createdAtMs: 1,
      updatedAtMs: 5,
    };
    for (const [index, toolName] of ['read', 'write'].entries()) {
      const id = `${toolName}-finished`;
      projection.activitiesById[id] = {
        id,
        turnId: 'turn-stopped',
        participantId: 'room-a:p1',
        sourceSessionId: 'room-a:s1',
        kind: 'participant_activity',
        status: 'completed',
        summary: `${toolName} 已返回`,
        payload: {
          rootId: 'turn-stopped',
          dispatchId: 'dispatch-stopped',
          sourceEventType: 'tool_finished',
          toolName,
          toolCallId: `call-${index + 1}`,
          arguments: { path: `…/project/file-${index + 1}.ts` },
          result: { outputPreview: `step ${index + 1}`, outputTruncated: false },
        },
        createdAtMs: index + 1,
      };
    }

    const { container } = render(
      <RoomTurn turnId="turn-stopped" room={room} projection={projection} personas={previewPersonas} />,
    );
    const user = userEvent.setup();
    const lane = container.querySelector<HTMLDetailsElement>('.room-agent-lane')!;
    await user.click(lane.querySelector(':scope > summary')!);
    const activity = container.querySelector<HTMLElement>('.room-agent-lane__activity')!;

    expect(activity).not.toHaveAttribute('open');
    await user.click(activity.querySelector('summary')!);
    expect(activity).toHaveTextContent('file-1.ts');
    expect(activity).toHaveTextContent('file-2.ts');
    expect(container.querySelector('.room-agent-lane')).toHaveAttribute('data-state', 'aborted');
    expect(screen.getByText('本轮已停止')).toBeInTheDocument();
    expect(screen.queryByText('全部完成')).not.toBeInTheDocument();
  });

  it('renders an interrupted Root tool as stopped instead of still running', async () => {
    const room = roomSummary('room-a', '中止工具状态 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-stopped-tool');
    projection.turnsById['turn-stopped-tool'] = {
      id: 'turn-stopped-tool',
      rootId: 'turn-stopped-tool',
      status: 'aborted',
      messageIds: [],
      activityIds: ['bash-interrupted'],
      participantIds: ['room-a:p1'],
      dispatchIds: ['dispatch-stopped-tool'],
      terminalParticipantIds: ['room-a:p1'],
      failedParticipantIds: [],
      abortedParticipantIds: ['room-a:p1'],
      terminalDispatchIds: ['dispatch-stopped-tool'],
      failedDispatchIds: [],
      abortedDispatchIds: ['dispatch-stopped-tool'],
      dispatchParticipantIds: { 'dispatch-stopped-tool': 'room-a:p1' },
      createdAtMs: 1,
      updatedAtMs: 5,
    };
    projection.activitiesById['bash-interrupted'] = {
      id: 'bash-interrupted',
      turnId: 'turn-stopped-tool',
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
      kind: 'participant_activity',
      status: 'aborted',
      summary: 'participant_activity',
      payload: {
        rootId: 'turn-stopped-tool',
        dispatchId: 'dispatch-stopped-tool',
        sourceEventType: 'tool_started',
        toolName: 'bash',
        toolCallId: 'call-interrupted',
        arguments: { command: 'pnpm run dev' },
      },
      createdAtMs: 2,
      updatedAtMs: 5,
    };

    const turnView = render(
      <RoomTurn turnId="turn-stopped-tool" room={room} projection={projection} personas={previewPersonas} />,
    );
    const user = userEvent.setup();
    const lane = turnView.container.querySelector<HTMLDetailsElement>('.room-agent-lane')!;
    await user.click(lane.querySelector(':scope > summary')!);
    const activity = turnView.container.querySelector<HTMLDetailsElement>('.room-agent-lane__activity')!;
    await user.click(activity.querySelector('summary')!);
    const toolCard = turnView.container.querySelector('.room-agent-activity--tool')!;
    expect(toolCard).toHaveAttribute('data-state', 'aborted');
    expect(toolCard).toHaveTextContent('已停止');
    expect(toolCard).not.toHaveTextContent('进行中');
    turnView.unmount();

    const statusView = render(
      <TooltipProvider><RoomStatusPanel room={room} projection={projection} open onClose={() => undefined} /></TooltipProvider>,
    );
    const statusActivity = statusView.container.querySelector('.room-status-activity')!;
    expect(statusActivity).toHaveAttribute('data-state', 'aborted');
    expect(statusActivity).toHaveTextContent('已停止');
    expect(statusActivity).not.toHaveTextContent('进行中');
  });

  it('keeps a transient resume lane visible without manufacturing a delivery outcome', () => {
    const room = roomSummary('room-a', '恢复分工终态 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-completed');
    projection.turnsById['turn-completed'] = {
      id: 'turn-completed',
      rootId: 'turn-completed',
      status: 'completed',
      messageIds: [],
      activityIds: ['resume-work'],
      participantIds: ['room-a:p1'],
      dispatchIds: ['dispatch-primary', 'dispatch-resume'],
      terminalParticipantIds: ['room-a:p1'],
      failedParticipantIds: [],
      abortedParticipantIds: [],
      terminalDispatchIds: ['dispatch-primary'],
      failedDispatchIds: [],
      abortedDispatchIds: [],
      dispatchParticipantIds: {
        'dispatch-primary': 'room-a:p1',
        'dispatch-resume': 'room-a:p1',
      },
      createdAtMs: 1,
      updatedAtMs: 5,
    };
    projection.activitiesById['resume-work'] = {
      id: 'resume-work',
      turnId: 'turn-completed',
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
      kind: 'participant_activity',
      status: 'completed',
      summary: '协作进度已经同步',
      payload: {
        rootId: 'turn-completed',
        dispatchId: 'dispatch-resume',
        activityKind: 'work',
        phase: 'completed',
      },
      createdAtMs: 4,
    };

    const { container } = render(
      <RoomTurn turnId="turn-completed" room={room} projection={projection} personas={previewPersonas} />,
    );
    const lane = container.querySelector('.room-agent-lane')!;

    expect(lane).toHaveAttribute('data-state', 'completed');
    expect(lane.querySelector('.room-agent-lane__identity')).toHaveTextContent('已完成');
    expect(lane.querySelector('.room-agent-lane__identity')).not.toHaveTextContent('执行中');
    expect(screen.queryByText('这轮回复已结束')).not.toBeInTheDocument();
    expect(screen.queryByText(/结构化交付回执/)).not.toBeInTheDocument();
    expect(screen.queryByText('这轮协作已完成')).not.toBeInTheDocument();
  });

  it('keeps the current Room lane truthful across a live Root terminal transition without avatars', () => {
    const room = roomSummary('room-a', '实时终态 Room');
    const projection = createRoomProjection(room.id);
    const liveAtMs = Date.now();
    projection.turnOrder.push('turn-live');
    projection.turnsById['turn-live'] = {
      id: 'turn-live',
      rootId: 'turn-live',
      status: 'running',
      messageIds: [],
      activityIds: ['route-live'],
      participantIds: ['room-a:p1'],
      dispatchIds: ['dispatch-live'],
      terminalParticipantIds: [],
      failedParticipantIds: [],
      abortedParticipantIds: [],
      terminalDispatchIds: [],
      failedDispatchIds: [],
      abortedDispatchIds: [],
      dispatchParticipantIds: { 'dispatch-live': 'room-a:p1' },
      createdAtMs: liveAtMs - 1_000,
      updatedAtMs: liveAtMs,
    };
    projection.activitiesById['route-live'] = {
      id: 'route-live',
      turnId: 'turn-live',
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
      kind: 'route_decision',
      status: 'completed',
      summary: '澄·今已接手',
      payload: {
        rootId: 'turn-live',
        dispatchId: 'dispatch-live',
        targetParticipantId: 'room-a:p1',
      },
      createdAtMs: liveAtMs,
    };
    const roomTurn = () => (
      <RoomTurn turnId="turn-live" room={room} projection={projection} personas={previewPersonas} />
    );
    const view = render(roomTurn());

    expect(view.container.querySelector('.room-turn')).toHaveAttribute('data-turn-status', 'running');
    expect(view.container.querySelector('.agent-persona-avatar')).not.toBeInTheDocument();
    expect(view.container.querySelector('.room-agent-lane')).toHaveAttribute('data-state', 'running');
    expect(view.container.querySelector('.room-agent-lane')).not.toHaveAttribute('open');
    expect(view.container.querySelector('.room-agent-lane__work')).toHaveTextContent('已接手');
    projection.turnsById['turn-live'] = {
      ...projection.turnsById['turn-live'],
      status: 'completed',
      terminalParticipantIds: ['room-a:p1'],
      terminalDispatchIds: ['dispatch-live'],
      updatedAtMs: liveAtMs + 1,
    };
    view.rerender(roomTurn());

    expect(view.container.querySelector('.room-turn__terminal')).not.toBeInTheDocument();
    expect(view.container.querySelector('.agent-persona-avatar')).not.toBeInTheDocument();
    expect(view.container.querySelector('.room-agent-lane')).toHaveAttribute('data-state', 'completed');
    expect(view.container.querySelector('.room-agent-lane')).not.toHaveAttribute('open');
    view.unmount();

    const settled = render(roomTurn());
    expect(settled.container.querySelector('.room-turn__terminal')).not.toBeInTheDocument();
  });

  it('keeps a committed lane completed without showing a failed read as successful', async () => {
    const room = roomSummary('room-a', '可恢复工具失败 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-a');
    projection.messageOrder.push('post-a');
    projection.activityOrder.push('route-a', 'tool-failed-a');
    projection.turnsById['turn-a'] = {
      id: 'turn-a',
      rootId: 'turn-a',
      status: 'running',
      messageIds: ['post-a'],
      activityIds: ['route-a', 'tool-failed-a'],
      participantIds: ['room-a:p1'],
      dispatchIds: ['dispatch-a'],
      terminalParticipantIds: ['room-a:p1'],
      failedParticipantIds: [],
      abortedParticipantIds: [],
      terminalDispatchIds: ['dispatch-a'],
      failedDispatchIds: [],
      abortedDispatchIds: [],
      dispatchParticipantIds: { 'dispatch-a': 'room-a:p1' },
      createdAtMs: 1,
      updatedAtMs: 4,
    };
    projection.messagesById['post-a'] = {
      id: 'post-a',
      roomId: room.id,
      turnId: 'turn-a',
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
      role: 'assistant',
      status: 'completed',
      text: '最终交付',
      projectionKind: 'post',
      postKind: 'result',
      rootId: 'turn-a',
      dispatchId: 'dispatch-a',
      createdAtMs: 4,
      completedAtMs: 4,
    };
    projection.activitiesById['route-a'] = {
      id: 'route-a',
      turnId: 'turn-a',
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
      kind: 'route_decision',
      status: 'completed',
      summary: '澄 已接手',
      payload: { rootId: 'turn-a', dispatchId: 'dispatch-a', targetParticipantId: 'room-a:p1' },
      createdAtMs: 1,
    };
    projection.activitiesById['tool-failed-a'] = {
      id: 'tool-failed-a',
      turnId: 'turn-a',
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
      kind: 'participant_activity',
      status: 'failed',
      summary: '首次参数不完整，已由 Agent 修正后重试',
      payload: {
        rootId: 'turn-a',
        dispatchId: 'dispatch-a',
        sourceEventType: 'tool_finished',
        toolName: 'read',
        arguments: { path: '…/project/game.js' },
        error: '文件不存在',
        isError: true,
      },
      createdAtMs: 2,
      updatedAtMs: 3,
    };

    const { container } = render(
      <RoomTurn turnId="turn-a" room={room} projection={projection} personas={previewPersonas} />,
    );

    expect(screen.getByText('最终交付')).toBeInTheDocument();
    expect(screen.getAllByText('已完成')).not.toHaveLength(0);
    expect(screen.queryByText('未完成')).not.toBeInTheDocument();
    expect(container.querySelector('.room-agent-lane')).toHaveAttribute('data-state', 'completed');
    const activity = container.querySelector<HTMLDetailsElement>('.room-agent-lane__activity')!;
    await userEvent.setup().click(activity.querySelector('summary')!);
    expect(container).toHaveTextContent('读取文件执行失败');
    expect(container).not.toHaveTextContent('game.js 已读取');
  });

  it('does not render Room lifecycle events without a public Root as a chat turn', () => {
    const projection = createRoomProjection('room-a');
    projection.turnOrder.push('unscoped');
    projection.turnsById.unscoped = {
      id: 'unscoped', status: 'completed', messageIds: [], activityIds: ['room-created'],
      participantIds: [], createdAtMs: 1, updatedAtMs: 2,
    };
    projection.activitiesById['room-created'] = {
      id: 'room-created', turnId: 'unscoped', participantId: null, sourceSessionId: '',
      kind: 'participant_status', status: 'completed', summary: '协作空间已就绪',
      payload: { status: 'room_created' }, createdAtMs: 1,
    };

    const { container } = render(<RoomTurn
      turnId="unscoped"
      projection={projection}
      personas={previewPersonas}
    />);

    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText('正在选择伙伴')).not.toBeInTheDocument();
  });

  it('shows humane terminal fallbacks and retries the owning message when allowed', async () => {
    const room = roomSummary('room-a', '失败恢复 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-failed');
    projection.messageOrder.push('user-failed');
    projection.turnsById['turn-failed'] = {
      id: 'turn-failed',
      status: 'failed',
      messageIds: ['user-failed'],
      activityIds: [],
      participantIds: [],
      failure: 'dispatchId: dispatch-private /Volumes/private/report',
      createdAtMs: 1,
      updatedAtMs: 2,
    };
    projection.messagesById['user-failed'] = {
      id: 'user-failed',
      roomId: room.id,
      turnId: 'turn-failed',
      participantId: null,
      sourceSessionId: '',
      role: 'user',
      status: 'completed',
      text: '请重新核对交付状态',
      createdAtMs: 1,
    };
    const onRetryTurn = vi.fn();
    const user = userEvent.setup();
    const roomTurn = (allowRetry = true) => <RoomTurn
      turnId="turn-failed"
      room={room}
      projection={projection}
      personas={previewPersonas}
      onRetryTurn={allowRetry ? onRetryTurn : undefined}
    />;
    const view = render(roomTurn());

    expect(screen.getByText('本轮未完成')).toBeInTheDocument();
    expect(screen.getByText('本轮未能完成；可以保留原请求并开始一次新的尝试。')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '再试一次' }));
    expect(onRetryTurn).toHaveBeenCalledWith('请重新核对交付状态', 'turn-failed');

    projection.turnsById['turn-failed'].status = 'aborted';
    view.rerender(roomTurn());
    expect(screen.getByText('本轮已停止')).toBeInTheDocument();
    expect(screen.getByText('未完成的伙伴、工具和后续任务不会继续。')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '再试一次' }));
    expect(onRetryTurn).toHaveBeenCalledTimes(2);

    view.rerender(roomTurn(false));
    expect(screen.queryByRole('button', { name: '再试一次' })).not.toBeInTheDocument();
  });

  it('coalesces a provider terminal message and turn_failed activity into one Room terminal', () => {
    const room = roomSummary('room-a', '单一失败终态 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-provider-failed');
    projection.messageOrder.push('user-provider-failed', 'assistant-provider-failed');
    projection.activityOrder.push('route-provider-failed', 'activity-provider-failed');
    projection.turnsById['turn-provider-failed'] = {
      id: 'turn-provider-failed', status: 'failed',
      messageIds: ['user-provider-failed', 'assistant-provider-failed'],
      activityIds: ['route-provider-failed', 'activity-provider-failed'],
      participantIds: ['room-a:p1'],
      dispatchIds: ['dispatch-provider-failed'],
      terminalDispatchIds: ['dispatch-provider-failed'],
      failedDispatchIds: ['dispatch-provider-failed'],
      dispatchParticipantIds: { 'dispatch-provider-failed': 'room-a:p1' },
      failure: '模型服务未能生成最终回复。',
      createdAtMs: 1, updatedAtMs: 4,
    };
    projection.messagesById['user-provider-failed'] = {
      id: 'user-provider-failed', roomId: room.id, turnId: 'turn-provider-failed',
      participantId: null, sourceSessionId: '', role: 'user', status: 'completed',
      text: '大家好', rootId: 'turn-provider-failed', createdAtMs: 1,
    };
    projection.messagesById['assistant-provider-failed'] = {
      id: 'assistant-provider-failed', roomId: room.id, turnId: 'turn-provider-failed',
      participantId: 'room-a:p1', sourceSessionId: 'room-a:s1', role: 'assistant', status: 'failed',
      text: '进度更新：模型服务未能生成最终回复。', rootId: 'turn-provider-failed',
      dispatchId: 'dispatch-provider-failed', createdAtMs: 2,
      message: {
        schemaVersion: 'rag-ime.agent-message.v1', id: 'assistant-provider-failed',
        sessionId: 'room-a:s1', turnId: 'turn-provider-failed', role: 'assistant', status: 'failed',
        blocks: [{
          id: 'assistant-provider-failed:error', type: 'error', status: 'failed',
          presentationKind: 'error', data: { message: '模型服务未能生成最终回复。' },
        }],
        attachments: [], citations: [], createdAtMs: 2, completedAtMs: 3,
      },
    };
    projection.activitiesById['route-provider-failed'] = {
      id: 'route-provider-failed', turnId: 'turn-provider-failed', participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1', kind: 'route_decision', status: 'completed', summary: '已接手',
      payload: { rootId: 'turn-provider-failed', dispatchId: 'dispatch-provider-failed' }, createdAtMs: 2,
    };
    projection.activitiesById['activity-provider-failed'] = {
      id: 'activity-provider-failed', turnId: 'turn-provider-failed', participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1', kind: 'turn_failed', status: 'failed', summary: '模型响应失败',
      payload: { rootId: 'turn-provider-failed', dispatchId: 'dispatch-provider-failed' }, createdAtMs: 3,
    };

    const { container } = render(<RoomTurn
      turnId="turn-provider-failed"
      room={room}
      projection={projection}
      personas={previewPersonas}
    />);

    expect(screen.getAllByText('大家好')).toHaveLength(1);
    expect(screen.getAllByText('本轮未完成')).toHaveLength(1);
    expect(screen.queryByText('进度更新：模型服务未能生成最终回复。')).not.toBeInTheDocument();
    expect(container.querySelectorAll('.room-turn__terminal')).toHaveLength(1);
    expect(container.querySelector('.room-agent-lane')).not.toBeInTheDocument();
  });

  it('keeps a retry-wait Root cancellable and unlocks it when the participant completes', () => {
    const rootTurnId = 'room-turn:retry-wait';
    let projection = reduceRoomEvent(
      createRoomProjection('room-a'),
      parseRoomEvent(roomEvent('room-a', 1, 'participant_activity', {
        rootId: rootTurnId,
        dispatchId: 'dispatch:retry-wait',
        sourceEventType: 'turn_failed',
        status: 'retry_wait',
        retryAttempt: 2,
        summary: '模型连接中断，已进入有界重试等待',
        retryDelayMs: 1_000,
      }, {
        turnId: rootTurnId,
        participantId: 'room-a:p1',
        sourceSessionId: 'room-a:s1',
      })),
    ).state;
    const onAbortTurn = vi.fn();
    const room = roomSummary('room-a', '重试等待 Room');
    const roomTurn = () => <RoomTurn
      turnId={rootTurnId}
      roomId="room-a"
      room={room}
      projection={projection}
      personas={previewPersonas}
      onAbortTurn={onAbortTurn}
    />;

    const view = render(roomTurn());
    expect(screen.getAllByText('Earth 正在等待第 2 次尝试')).toHaveLength(2);
    expect(screen.getByText(/1s 后重试/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '停止本轮任务' }));
    expect(onAbortTurn).toHaveBeenCalledWith(rootTurnId);

    projection = reduceRoomEvent(
      projection,
      parseRoomEvent(roomEvent('room-a', 2, 'turn_completed', {
        rootId: rootTurnId,
        dispatchId: 'dispatch:retry-wait',
        status: 'completed',
      }, {
        turnId: rootTurnId,
        participantId: 'room-a:p1',
        sourceSessionId: 'room-a:s1',
      })),
    ).state;
    view.rerender(roomTurn());

    expect(projection.turnsById[rootTurnId]).toMatchObject({
      status: 'completed',
      terminalDispatchIds: ['dispatch:retry-wait'],
      failedDispatchIds: [],
      terminalParticipantIds: ['room-a:p1'],
      failedParticipantIds: [],
    });
    expect(screen.queryByText('这轮回复已结束')).not.toBeInTheDocument();
    expect(screen.queryByText('这轮协作已完成')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '停止本轮任务' })).not.toBeInTheDocument();
  });

  it('sends a Room retry with an authoritative predecessor link', async () => {
    const rootTurnId = 'room-turn:failed-for-retry';
    const events = [
      roomEvent('room-a', 1, 'user_message', {
        text: '保留这一条用户请求',
        clientMessageId: 'room-original-message',
        dispatches: [{ dispatchId: 'dispatch-failed-for-retry', participantId: 'room-a:p1' }],
      }, { turnId: rootTurnId }),
      roomEvent('room-a', 2, 'route_decision', {
        rootId: rootTurnId,
        dispatchId: 'dispatch-failed-for-retry',
        targetParticipantId: 'room-a:p1',
      }, { turnId: rootTurnId, participantId: 'room-a:p1', sourceSessionId: 'room-a:s1' }),
      roomEvent('room-a', 3, 'turn_failed', {
        rootId: rootTurnId,
        dispatchId: 'dispatch-failed-for-retry',
        error: '模型服务未能生成最终回复。',
      }, { turnId: rootTurnId, participantId: 'room-a:p1', sourceSessionId: 'room-a:s1' }),
    ];
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '原位重试 Room')] },
      'agent.room.snapshot': roomSnapshot('room-a', events),
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.sessions.list': { ok: true, items: [] },
      'agent.room.message': { ok: true },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('button', { name: '再试一次' }));

    await waitFor(() => {
      const request = transport.requests.find((call) => call.request.pathId === 'agent.room.message')?.request;
      expect(request?.body).toMatchObject({
        message: '保留这一条用户请求',
        retryOfRootId: rootTurnId,
      });
    });
    expect(screen.getAllByText('保留这一条用户请求')).toHaveLength(1);
  });

  it('stops the whole server root through one Room cancellation command', async () => {
    const rootTurnId = 'room-turn:root-stop-1';
    const runningEvent = {
      ...roomEvent('room-a', 1, 'participant_status', {
        status: 'accepted',
        summary: '两位 Agent 已接手',
      }),
      turnId: rootTurnId,
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
    };
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '整轮停止')] },
      'agent.room.snapshot': roomSnapshot('room-a', [runningEvent]),
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.abort': {
        schemaVersion: 'rag-ime.agent-room-abort.v1',
        ok: true,
        roomId: 'room-a',
        roomTurnId: rootTurnId,
        status: 'terminated',
        cancellationReceiptId: 'room-cancel:1',
        surfaces: {},
        pendingTargets: [],
      },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('button', { name: '停止本轮任务' }));

    await waitFor(() => {
      const request = transport.requests.find(
        (call) => call.request.pathId === 'agent.room.abort',
      )?.request;
      expect(request?.params).toEqual({ roomId: 'room-a' });
      expect(request?.body).toMatchObject({ roomTurnId: rootTurnId });
      expect(String((request?.body as Record<string, unknown>)?.clientRequestId))
        .toMatch(/^room-abort-/);
    });
    expect(screen.queryByRole('button', { name: '停止本轮任务' })).not.toBeInTheDocument();
  });

  it('keeps a root visibly active when any cancellation surface is unverified', async () => {
    const rootTurnId = 'room-turn:root-stop-pending';
    const runningEvent = {
      ...roomEvent('room-a', 1, 'participant_status', {
        status: 'accepted',
        summary: 'Agent 已接手',
      }),
      turnId: rootTurnId,
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
    };
    const transport = new MockControlTransport({ routes: {
      'agent.rooms.list': { ok: true, items: [roomSummary('room-a', '停止待确认')] },
      'agent.room.snapshot': roomSnapshot('room-a', [runningEvent]),
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.room.abort': {
        schemaVersion: 'rag-ime.agent-room-abort.v1',
        ok: false,
        roomId: 'room-a',
        roomTurnId: rootTurnId,
        status: 'cancellation_pending',
        cancellationReceiptId: 'room-cancel:pending',
        surfaces: {},
        pendingTargets: [
          { surface: 'provider', state: 'requested', targetIds: ['provider:1'] },
          { surface: 'shell', state: 'unknown', targetIds: ['shell:1', 'shell:2'] },
        ],
      },
    } });
    const user = userEvent.setup();
    render(<ControlTransportProvider transport={transport}><TooltipProvider><RoomsFeature /></TooltipProvider></ControlTransportProvider>);

    await user.click(await screen.findByRole('button', { name: '停止本轮任务' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      '仍在确认：模型生成、命令行进程（2 项）',
    );
    expect(screen.getByRole('button', { name: '停止本轮任务' })).toBeEnabled();
  });

  it('shows only explicit Posts and hides internal Room execution events', () => {
    const room = roomSummary('room-a', '人类语言 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-a');
    projection.activityOrder.push('route', 'hidden-tool', 'raw');
    projection.turnsById['turn-a'] = {
      id: 'turn-a', status: 'running', messageIds: ['assistant'], activityIds: ['route', 'hidden-tool', 'raw'],
      participantIds: ['room-a:p1'], createdAtMs: 1, updatedAtMs: 1,
    };
    projection.messageOrder.push('assistant');
    projection.messagesById.assistant = {
      id: 'assistant', roomId: room.id, turnId: 'turn-a', participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1', role: 'assistant', status: 'completed', text: '公开回答', createdAtMs: 1,
      message: {
        schemaVersion: 'rag-ime.agent-message.v1', id: 'assistant', sessionId: 'room-a:s1',
        turnId: 'turn-a', role: 'assistant', status: 'completed', attachments: [], citations: [], createdAtMs: 1,
        blocks: [
          { id: 'reasoning', type: 'reasoning_summary', status: 'completed', presentationKind: 'reasoning_summary', data: { text: '秘密思考摘要' } },
          { id: 'text', type: 'text', status: 'completed', presentationKind: 'markdown', data: { text: '公开回答' } },
          { id: 'status', type: 'status', status: 'completed', presentationKind: 'status.v1', visibility: 'room_post', data: { title: '交付状态', state: 'completed', summary: '已通过刷新恢复' } },
          { id: 'checklist', type: 'checklist', status: 'completed', presentationKind: 'checklist.v1', visibility: 'room_post', data: { title: '公开验收', items: [{ id: 'room-post', text: 'Room Post 可重渲染', checked: true }] } },
          { id: 'private-card', type: 'card', status: 'completed', presentationKind: 'card.v1', visibility: 'private_session', data: { title: '私有 Session 中间稿', bodyMarkdown: '不应进入 Posts' } },
        ],
      },
    };
    projection.activitiesById.route = {
      id: 'route', turnId: 'turn-a', participantId: 'room-a:p1', sourceSessionId: 'room-a:s1',
      kind: 'route_decision', status: 'completed', summary: 'route_decision',
      payload: { routingPolicy: 'moderator', targetDisplayName: '澄' }, createdAtMs: 1,
    };
    projection.activitiesById['hidden-tool'] = {
      id: 'hidden-tool', turnId: 'turn-a', participantId: 'room-a:p1', sourceSessionId: 'room-a:s1',
      kind: 'participant_activity', status: 'completed', summary: '已完成一项内部工具步骤',
      payload: {}, createdAtMs: 1,
    };
    projection.activitiesById.raw = {
      id: 'raw', turnId: 'turn-a', participantId: 'room-a:p1', sourceSessionId: 'room-a:s1',
      kind: 'participant_activity', status: 'running', summary: 'participant_activity',
      payload: {}, createdAtMs: 1,
    };

    const { container } = render(<RoomTurn turnId="turn-a" room={room} projection={projection} personas={previewPersonas} />);
    expect(screen.getByText('公开回答')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: '交付状态' })).toHaveTextContent('已通过刷新恢复');
    expect(screen.getByText('Room Post 可重渲染')).toBeInTheDocument();
    expect(container).not.toHaveTextContent('私有 Session 中间稿');
    expect(container).not.toHaveTextContent('不应进入 Posts');
    expect(container.querySelector('.room-group-activity')).not.toBeInTheDocument();
    expect(container).not.toHaveTextContent('秘密思考摘要');
    expect(container).not.toHaveTextContent('participant_activity');
    expect(container).not.toHaveTextContent('route_decision');
  });

  it('shows WorkItem responsibility, owner, review state, and reachable acceptance criteria in the Room status panel', async () => {
    const room = roomSummary('room-a', '责任 Room');
    room.workItems = [{
      id: 'room-work:1',
      roomId: room.id,
      topicId: '',
      rootTurnId: 'turn-a',
      rootWorkId: 'room-work:1',
      parentWorkId: '',
      objective: '核对多端网关回放边界',
      expectedOutput: '测试与风险说明',
      acceptanceCriteria: ['回放不重复'],
      accountableParticipantId: 'room-a:p1',
      currentOwnerParticipantId: 'room-a:p2',
      offeredToParticipantId: '',
      createdByParticipantId: 'room-a:p1',
      clientMessageId: 'test-work-1',
      state: 'review',
      depth: 1,
      revision: 1,
      resultSummary: '已完成',
      artifactRefs: [],
      evidenceRefs: ['test:room-replay'],
      blocker: {},
      acceptedTurnId: 'turn-worker',
      createdAtMs: 1,
      updatedAtMs: 2,
      completedAtMs: null,
    }];

    const user = userEvent.setup();
    render(<TooltipProvider><RoomStatusPanel room={room} projection={createRoomProjection(room.id)} open onClose={() => undefined} /></TooltipProvider>);

    expect(screen.getByText('任务分工')).toBeInTheDocument();
    const workRow = screen.getAllByText('核对多端网关回放边界')
      .map((element) => element.closest<HTMLElement>('.room-status-work__item'))
      .find(Boolean)!;
    expect(within(workRow).getAllByText('等待汇合')).not.toHaveLength(0);
    expect(within(workRow).getByText('Mars')).toBeInTheDocument();
    expect(within(workRow).getByText('Earth')).toBeInTheDocument();
    expect(within(workRow).getByText('第 1 次')).toBeInTheDocument();
    expect(within(workRow).getByText('测试与风险说明')).toBeInTheDocument();
    const acceptanceSummary = within(workRow).getByText('查看验收条件').closest('summary')!;
    const acceptanceDisclosure = acceptanceSummary.closest('details')!;
    expect(acceptanceDisclosure).toHaveClass('ui-disclosure');
    expect(acceptanceDisclosure.querySelector('.ui-disclosure__reveal')).toHaveAttribute('inert');
    await user.click(acceptanceSummary);
    expect(acceptanceSummary).toHaveAttribute('aria-expanded', 'true');
    expect(within(acceptanceDisclosure).getByText('回放不重复')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /协作规则/ })).not.toBeInTheDocument();
    expect(screen.queryByText('一起检查的人始终明确')).not.toBeInTheDocument();
    expect(screen.queryByText('不会无限循环')).not.toBeInTheDocument();
    expect(screen.getByText('调研与证据 · 已加入')).toBeInTheDocument();
    expect(screen.queryByText(/只读调研/)).not.toBeInTheDocument();
  });

  it('opens an equal Room participant as its own PAWOS window', async () => {
    const room = roomSummary('room-a', '伙伴窗口 Room');
    const requests: PawOsWindowRequest[] = [];
    const user = userEvent.setup();
    render(
      <TooltipProvider>
        <PawOsDesktopProvider openWindow={(request) => requests.push(request)}>
          <RoomStatusPanel room={room} projection={createRoomProjection(room.id)} open onClose={() => undefined} />
        </PawOsDesktopProvider>
      </TooltipProvider>,
    );

    await user.click(screen.getByRole('button', { name: '伙伴状态 2' }));
    await user.click(screen.getByRole('button', { name: '打开 Earth 伙伴窗口' }));

    /* planet 窗口统一铭牌：标题是行星名，分工进副标题，
       不再携带旧角色名或机器 Session id。 */
    expect(requests).toEqual([{
      appId: 'agent',
      background: false,
      target: expect.objectContaining({
        kind: 'participant',
        id: 'room-a:p1',
        roomId: 'room-a',
        title: 'Earth',
        subtitle: '最终汇合与回复',
      }),
    }]);
  });

  it('keeps every Room WorkItem reachable in the scrollable status body', () => {
    const room = roomSummary('room-a', '完整任务列表 Room');
    room.workItems = Array.from({ length: 9 }, (_, index) => roomWorkItemFixture(
      room.id,
      `work:${index + 1}`,
      { state: index === 8 ? 'review' : 'queued' },
    ));

    render(<TooltipProvider><RoomStatusPanel room={room} projection={createRoomProjection(room.id)} open onClose={() => undefined} /></TooltipProvider>);

    expect(screen.getAllByText('work:9')).not.toHaveLength(0);
    expect(screen.getAllByText('任务分工')).toHaveLength(1);
  });

  it('collapses repeated Room status updates into one readable sidebar step', () => {
    const room = roomSummary('room-a', '状态聚合 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-a');
    projection.turnsById['turn-a'] = {
      id: 'turn-a',
      status: 'completed',
      messageIds: [],
      activityIds: ['status-1', 'status-2', 'status-3'],
      participantIds: ['room-a:p1'],
      createdAtMs: 1,
      updatedAtMs: 4,
    };
    for (const [index, id] of ['status-1', 'status-2', 'status-3'].entries()) {
      projection.activitiesById[id] = {
        id,
        turnId: 'turn-a',
        participantId: 'room-a:p1',
        sourceSessionId: 'room-a:s1',
        kind: 'participant_status',
        status: 'completed',
        summary: '协作状态已经同步',
        payload: {},
        createdAtMs: index + 1,
      };
    }
    const { container } = render(
      <TooltipProvider><RoomStatusPanel room={room} projection={projection} open onClose={() => undefined} /></TooltipProvider>,
    );

    expect(container.querySelectorAll('.room-status-activity')).toHaveLength(1);
    expect(container.querySelector('.room-status-activity')).toHaveTextContent('状态同步');
    expect(container.querySelector('.room-status-activity')).toHaveTextContent('合并 3 次更新');
  });

  it('does not present automatic Room policy authorization as a user-facing step', () => {
    const room = roomSummary('room-a', '默认执行 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-a');
    projection.turnsById['turn-a'] = {
      id: 'turn-a',
      status: 'completed',
      messageIds: [],
      activityIds: ['policy-approval'],
      participantIds: ['room-a:p1'],
      createdAtMs: 1,
      updatedAtMs: 2,
    };
    projection.activitiesById['policy-approval'] = {
      id: 'policy-approval',
      turnId: 'turn-a',
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
      kind: 'participant_activity',
      status: 'completed',
      summary: '安全策略已自动处理这次操作',
      payload: {
        sourceEventType: 'approval_resolved',
        approvalId: 'approval:auto',
        toolCallId: 'tool:auto',
        automatic: true,
        decisionMode: 'policy',
        state: 'applied',
      },
      createdAtMs: 1,
    };

    const { container } = render(
      <TooltipProvider><RoomStatusPanel room={room} projection={projection} open onClose={() => undefined} /></TooltipProvider>,
    );

    expect(container.querySelectorAll('.room-status-activity')).toHaveLength(0);
    expect(container).not.toHaveTextContent('安全审批');
    expect(container).not.toHaveTextContent('安全策略已自动处理这次操作');
  });

  it('keeps a merged automatic-policy Tool failure in the Room status panel', () => {
    const room = roomSummary('room-a', '默认执行 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-a');
    projection.turnsById['turn-a'] = {
      id: 'turn-a',
      status: 'failed',
      messageIds: [],
      activityIds: ['tool-failed'],
      participantIds: ['room-a:p1'],
      createdAtMs: 1,
      updatedAtMs: 2,
    };
    projection.activitiesById['tool-failed'] = {
      id: 'tool-failed',
      turnId: 'turn-a',
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
      kind: 'participant_activity',
      status: 'failed',
      summary: '运行项目命令失败',
      payload: {
        sourceEventType: 'tool_finished',
        toolCallId: 'tool:failed',
        toolName: 'workspace_shell',
        approvalId: 'approval:auto',
        automatic: true,
        decisionMode: 'policy',
        state: 'failed',
        error: 'exit code 1',
      },
      createdAtMs: 1,
    };

    const { container } = render(
      <TooltipProvider><RoomStatusPanel room={room} projection={projection} open onClose={() => undefined} /></TooltipProvider>,
    );

    expect(container).toHaveTextContent('运行项目命令失败');
  });

  it('keeps the 765x1568 status panel in normal top-to-bottom flow while a review waits', () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 765 });
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 1_568 });
    const room = roomSummary('room-a', '窄屏审阅 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-a');
    projection.turnsById['turn-a'] = {
      id: 'turn-a',
      status: 'completed',
      messageIds: [],
      activityIds: ['plan-review'],
      participantIds: ['room-a:p1'],
      createdAtMs: 1,
      updatedAtMs: 2,
    };
    projection.activitiesById['plan-review'] = {
      id: 'plan-review',
      turnId: 'turn-a',
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
      kind: 'participant_activity',
      status: 'waiting',
      summary: '等待计划审阅',
      payload: { requestKind: 'plan_review', requestId: 'plan-review' },
      createdAtMs: 1,
    };

    const { container } = render(
      <TooltipProvider><RoomStatusPanel room={room} projection={projection} open onClose={() => undefined} /></TooltipProvider>,
    );
    const body = container.querySelector<HTMLElement>('.agent-status-panel__body')!;
    const sections = [...body.querySelectorAll<HTMLElement>('.agent-status-section')];
    expect(getComputedStyle(body).display).toBe('flex');
    expect(getComputedStyle(body).flexDirection).toBe('column');
    expect(getComputedStyle(body).justifyContent).toBe('flex-start');
    expect(sections.every((section) => getComputedStyle(section).flexGrow === '0')).toBe(true);
    expect(screen.getAllByText('等待审阅')).toHaveLength(2);
    expect(screen.queryByText('协作中')).not.toBeInTheDocument();
    expect(screen.queryByText('已完成')).not.toBeInTheDocument();
  });

  it('keeps a running Room active after a recoverable tool failure', () => {
    const room = roomSummary('room-a', '恢复中 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-a');
    projection.turnsById['turn-a'] = {
      id: 'turn-a',
      status: 'running',
      messageIds: [],
      activityIds: ['tool-failed'],
      participantIds: ['room-a:p1'],
      createdAtMs: 1,
      updatedAtMs: 2,
    };
    projection.activitiesById['tool-failed'] = {
      id: 'tool-failed',
      turnId: 'turn-a',
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
      kind: 'tool_call',
      status: 'failed',
      summary: '运行命令失败',
      payload: { toolName: 'workspace_shell' },
      createdAtMs: 2,
    };

    const { container } = render(
      <TooltipProvider>
        <RoomStatusPanel room={room} projection={projection} open onClose={() => undefined} />
      </TooltipProvider>,
    );

    expect(container.querySelector('.agent-status-turn')).toHaveAttribute('data-state', 'running');
    expect(screen.getAllByText('协作中')).not.toHaveLength(0);
    expect(container.querySelector('.room-status-activity')).toHaveAttribute('data-state', 'failed');
  });

  it('shows each partner work summary in an independently scrollable status feed', () => {
    const room = roomSummary('room-a', '公开进度 Room');
    const projection = createRoomProjection(room.id);
    projection.activityOrder.push('reasoning-a', 'tool-b');
    projection.activitiesById['reasoning-a'] = {
      id: 'reasoning-a',
      turnId: 'root-a',
      participantId: 'room-a:p1',
      sourceSessionId: 'room-a:s1',
      kind: 'participant_activity',
      status: 'running',
      summary: '正在核对恢复后的任务边界',
      payload: { rootId: 'root-a', sourceEventType: 'reasoning_summary' },
      createdAtMs: 2,
      updatedAtMs: 2,
    };
    projection.activitiesById['tool-b'] = {
      id: 'tool-b',
      turnId: 'root-a',
      participantId: 'room-a:p2',
      sourceSessionId: 'room-a:s2',
      kind: 'participant_activity',
      status: 'running',
      summary: '正在读取公开检查记录',
      payload: { rootId: 'root-a', sourceEventType: 'tool_progress' },
      createdAtMs: 3,
      updatedAtMs: 3,
    };

    const { container } = render(
      <TooltipProvider>
        <RoomStatusPanel room={room} projection={projection} open onClose={() => undefined} />
      </TooltipProvider>,
    );
    const feed = screen.getByLabelText('工作进展与运行记录');
    const lanes = feed.querySelectorAll('.room-status-public-lane');
    expect(lanes).toHaveLength(2);
    expect([...lanes].every((lane) => lane.className === 'room-status-public-lane')).toBe(true);
    expect(lanes[0]).toHaveTextContent('工作摘要');
    expect(lanes[0]).not.toHaveTextContent('伙伴自述');
    expect(lanes[1]).toHaveTextContent('运行记录 · 工具进度');
    expect(feed).toHaveAttribute('tabindex', '0');
    expect(feed).toHaveAttribute('aria-live', 'polite');
    expect(getComputedStyle(feed).overflowY).toBe('auto');
    expect(container).toHaveTextContent('正在核对恢复后的任务边界');
    expect(container).toHaveTextContent('正在读取公开检查记录');
    expect(container).toHaveTextContent('工作摘要');
  });








  it('labels a completed Room turn without a Post as completed in the status panel', () => {
    const room = roomSummary('room-a', '终态 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-a');
    projection.turnsById['turn-a'] = {
      id: 'turn-a',
      status: 'completed',
      messageIds: [],
      activityIds: [],
      participantIds: ['room-a:p1'],
      createdAtMs: 1,
      updatedAtMs: 2,
    };

    render(
      <TooltipProvider>
        <RoomStatusPanel room={room} projection={projection} open onClose={() => undefined} />
      </TooltipProvider>,
    );

    expect(screen.getAllByText('已完成')).not.toHaveLength(0);
    expect(screen.queryByText('等待后续')).not.toBeInTheDocument();
  });
  it('renders managed image blocks from a Room user-message receipt', () => {
    const room = roomSummary('room-media', '图片 Room');
    const event = parseRoomEvent(roomEvent(room.id, 1, 'user_message', {
      messageId: 'room-media-message',
      text: '请查看这张图',
      attachmentReceipts: [{
        mediaId: 'media_room_image_01',
        ownerType: 'room',
        roomId: room.id,
        fileName: 'diagram.png',
        mimeType: 'image/png',
        byteSize: 128,
        sha256: 'a'.repeat(64),
        width: 640,
        height: 480,
      }],
    }, { turnId: 'room-media-turn' }));
    const projection = reduceRoomEvent(createRoomProjection(room.id), event).state;

    render(<RoomTurn turnId="room-media-turn" room={room} projection={projection} personas={previewPersonas} />);

    expect(screen.getByRole('img', { name: 'diagram.png' })).toHaveAttribute(
      'src',
      '/api/agent/media/media_room_image_01/content?roomId=room-media',
    );
  });

  it('coalesces only a shared event alias while preserving a distinct publication', () => {
    const room = roomSummary('room-identity', '事件身份 Room');
    const projection = createRoomProjection(room.id);
    projection.turnOrder.push('turn-identity');
    projection.turnsById['turn-identity'] = {
      id: 'turn-identity',
      rootId: 'turn-identity',
      status: 'completed',
      messageIds: ['post-first', 'post-mirror', 'post-distinct'],
      activityIds: [],
      participantIds: ['room-identity:p1'],
      dispatchIds: ['dispatch-identity'],
      terminalParticipantIds: ['room-identity:p1'],
      failedParticipantIds: [],
      abortedParticipantIds: [],
      terminalDispatchIds: ['dispatch-identity'],
      failedDispatchIds: [],
      abortedDispatchIds: [],
      dispatchParticipantIds: { 'dispatch-identity': 'room-identity:p1' },
      createdAtMs: 1,
      updatedAtMs: 4,
    };
    const post = (id: string, sourceEventId: string, text: string, createdAtMs: number) => ({
      id,
      roomId: room.id,
      turnId: 'turn-identity',
      participantId: 'room-identity:p1',
      sourceSessionId: 'room-identity:s1',
      sourceEventId,
      role: 'assistant' as const,
      status: 'completed' as const,
      text,
      projectionKind: 'post' as const,
      rootId: 'turn-identity',
      dispatchId: 'dispatch-identity',
      createdAtMs,
      completedAtMs: createdAtMs,
    });
    projection.messageOrder.push('post-first', 'post-mirror', 'post-distinct');
    projection.messagesById['post-first'] = post('post-first', 'event-shared', '较早镜像', 1);
    projection.messagesById['post-mirror'] = post('post-mirror', 'event-shared', '同一事件的完整投影', 2);
    projection.messagesById['post-distinct'] = post('post-distinct', 'event-distinct', '同一事件的完整投影', 3);

    const { container } = render(
      <RoomTurn turnId="turn-identity" room={room} projection={projection} personas={previewPersonas} />,
    );

    const committedPosts = [...container.querySelectorAll('.room-agent-lane__post')];
    expect(committedPosts).toHaveLength(2);
    expect(committedPosts.every((post) => post.closest('.room-agent-lane'))).toBe(true);
    expect(container.querySelectorAll('.room-conversation-post')).toHaveLength(0);
    expect(container.querySelectorAll('.room-agent-lane')).toHaveLength(1);
    expect(container).not.toHaveTextContent('较早镜像');
    expect(screen.getAllByText('同一事件的完整投影')).toHaveLength(2);
  });

});

function pickedRoomImage(
  id: string,
  name: string,
  shaCharacter: string,
): PickedFile {
  return {
    id,
    name,
    mimeType: 'image/png',
    byteSize: 128,
    roomId: 'room-a',
    sha256: shaCharacter.repeat(64).slice(0, 64),
  };
}

function roomSummary(roomId: string, title: string): RoomSummary {
  return {
    id: roomId,
    title,
    status: 'active',
    executionMode: 'workspace_managed',
    permissionPolicy: {
      schemaVersion: 'rag-ime.room-permission-policy.v1',
      room: { executionMode: 'workspace_managed' },
      partner: { executionMode: 'inherit' },
      toolAgent: { executionMode: 'inherit' },
    },
    routingPolicy: 'moderator',
    moderatorParticipantId: `${roomId}:p1`,
    workspaceRoots: ['/Volumes/work/learnA'],
    updatedAtMs: 2,
    participants: [
      { id: `${roomId}:p1`, sessionId: `${roomId}:s1`, roleId: 'companion-present-v1', roleVersion: '1', displayName: '澄', collaborationRole: 'coordinator', status: 'active', ordinal: 0 },
      { id: `${roomId}:p2`, sessionId: `${roomId}:s2`, roleId: 'companion-firstlight-v1', roleVersion: '1', displayName: '澄·初', collaborationRole: 'researcher', status: 'active', ordinal: 1 },
    ],
  };
}

function roomWorkItemFixture(
  roomId: string,
  id: string,
  overrides: Partial<NonNullable<RoomSummary['workItems']>[number]> = {},
): NonNullable<RoomSummary['workItems']>[number] {
  return {
    id,
    roomId,
    topicId: '',
    rootTurnId: `${roomId}:turn-1`,
    rootWorkId: id,
    parentWorkId: '',
    objective: id,
    expectedOutput: '可核验交付',
    acceptanceCriteria: ['保留真实阶段与并行关系'],
    accountableParticipantId: `${roomId}:p1`,
    currentOwnerParticipantId: `${roomId}:p1`,
    offeredToParticipantId: '',
    createdByParticipantId: `${roomId}:p1`,
    clientMessageId: `client:${id}`,
    state: 'queued',
    depth: 1,
    revision: 1,
    resultSummary: '',
    artifactRefs: [],
    evidenceRefs: [],
    blocker: {},
    acceptedTurnId: `${roomId}:turn-1`,
    createdAtMs: 1,
    updatedAtMs: 2,
    completedAtMs: null,
    ...overrides,
  };
}

function roomWorkDocumentFixture(authorityId: string, title: string): WorkDocumentV1 {
  const documentId = `workdoc_${(authorityId === 'work:other' ? '2' : '1').repeat(32)}`;
  return {
    documentId,
    authorityKind: 'room_work_item',
    authorityId,
    authorityRevision: 1,
    authorityKey: `room_work_item:${authorityId}:1`,
    documentRevision: 2,
    contentSha256: 'a'.repeat(64),
    workspaceRoot: '/workspace',
    path: `/workspace/docs/active/${documentId}.md`,
    activePath: `/workspace/docs/active/${documentId}.md`,
    archivePath: `/workspace/docs/archive/${documentId}.md`,
    state: 'active',
    title,
    terminalReceiptId: '',
    error: '',
    createdAtMs: 1,
    updatedAtMs: 2,
  };
}

function roomWorkflowFixture(sessionId: string) {
  return {
    schemaVersion: 'rag-ime.agent-workflow-state.v1',
    ok: true,
    sessionId,
    todo: {
      schemaVersion: 'rag-ime.agent-todo.v1',
      id: `todo:${sessionId}`,
      sessionId,
      revision: 3,
      actor: 'agent',
      updatedAtMs: 3,
      roomLineage: null,
      phases: [{
        name: '实现',
        tasks: [
          { content: '读取真实 Room 事件', status: 'completed' },
          { content: '实现交互结果视图', status: 'in_progress' },
          { content: '完成网页验收', status: 'pending' },
        ],
      }],
      counts: { total: 3, pending: 1, inProgress: 1, completed: 1, abandoned: 0 },
    },
    goal: {
      schemaVersion: 'rag-ime.agent-goal.v1',
      sessionId,
      configured: false,
      goalId: '',
      revision: 0,
      objective: '',
      successCriteria: '',
      evidenceExpectations: [],
      status: 'cleared',
      budget: { tokenLimit: null, timeLimitMs: null },
      usage: { tokens: 0, elapsedMs: 0 },
      remaining: { tokens: null, timeMs: null },
      budgetExceeded: false,
      completionAudit: null,
      cancellationAudit: null,
      updatedAtMs: 0,
    },
    actGate: {
      allowed: true,
      reason: 'user_execution_request',
      message: '用户已请求执行。',
      todoRevision: 3,
      goalRevision: 0,
    },
  };
}

function roomSubagentListFixture(sessionId: string) {
  return {
    ok: true,
    items: [{
      schemaVersion: 'rag-ime.agent-subagent-batch.v1',
      id: `batch:${sessionId}`,
      parentSessionId: sessionId,
      parentRunId: 'run:parent',
      contextMode: 'fresh',
      resultDeliveryMode: 'inline',
      state: 'running',
      depth: 0,
      maxDepth: 2,
      abortRequested: false,
      causalMetadata: {
        todoId: `todo:${sessionId}`,
        todoRevision: 3,
        goalId: '',
        goalRevision: 0,
        roomBound: true,
        roomId: 'room-a',
        rootId: 'room-a:turn-1',
        taskId: 'task:html',
        dispatchId: 'dispatch:html',
        generation: 1,
      },
      createdAtMs: 1,
      updatedAtMs: 2,
      completedAtMs: null,
      runs: [{
        schemaVersion: 'rag-ime.agent-subagent-run.v1',
        id: 'run:html-review',
        nodeId: 'node:html-review',
        attemptId: 'attempt:html-review:1',
        attemptNumber: 1,
        predecessorAttemptId: '',
        ownerRunId: 'run:parent',
        parentRunId: 'run:parent',
        depth: 1,
        batchId: `batch:${sessionId}`,
        childSessionId: 'room-a:s2:child-1',
        todoTask: '实现交互结果视图',
        todoPhase: '实现',
        templateId: 'reviewer',
        templateVersion: '1',
        ordinal: 0,
        task: '验证 HTML 报告',
        expectedOutput: '浏览器验收结果',
        acceptanceCriteria: ['脚本和表单可以运行'],
        launchDigest: {
          schemaVersion: 'rag-ime.agent-subagent-launch-digest.v1',
          contextMode: 'fresh',
          templateId: 'reviewer',
          templateVersion: '1',
          modelProfile: 'openai-codex/gpt-5.6-sol',
          thinkingLevel: 'high',
          toolProfileVersion: 'subagent-readonly-v1',
          toolAllowlistMode: 'profile',
          tools: ['knowledge'],
          piSkillsEnabled: false,
          codexSkillsEnabled: false,
          workspaceAccess: 'read_only',
          workspaceRootCount: 1,
          outputContract: { required: false, schemaSha256: '' },
          extensionRuntime: 'pi_host_managed',
        },
        contract: {
          status: 'not_requested',
          error: '',
          toolCallId: '',
          validatedAtMs: null,
        },
        state: 'running',
        budget: { maxTurns: 8, maxToolCalls: 12, maxTotalTokens: 20_000, maxDurationMs: 300_000, maxOutputChars: 16_000 },
        usage: { turnCount: 1, toolCount: 1, totalTokens: 800 },
        result: {},
        error: '',
        resultContextScheduledAtMs: null,
        createdAtMs: 1,
        startedAtMs: 1,
        updatedAtMs: 2,
        completedAtMs: null as number | null,
      }],
    }],
  };
}


function roomSnapshot(roomId: string, events: ReturnType<typeof roomEvent>[], title?: string) {
  const room = roomSummary(roomId, title ?? (roomId === 'room-a' ? 'Room A' : roomId === 'room-b' ? 'Room B' : 'Room'));
  const lastSequence = events.at(-1)?.sequence ?? 0;
  return {
    schemaVersion: 'rag-ime.agent-room-snapshot.v1',
    ok: true,
    room: {
      ...room,
      schemaVersion: 'rag-ime.agent-room.v1',
      createdAtMs: 1,
      lastEventSequence: lastSequence,
      participants: room.participants.map((participant) => ({
        ...participant,
        schemaVersion: 'rag-ime.agent-participant.v1',
        roomId,
        createdAtMs: 1,
        lastSpokeAtMs: null,
      })),
    },
    events,
    firstSequence: events[0]?.sequence ?? 0,
    lastSequence,
    resumeToken: lastSequence ? `${roomId}:${lastSequence}` : '',
    truncated: (events[0]?.sequence ?? 0) > 1,
  };
}

function roomQuestionEvent(
  roomId: string,
  sequence: number,
  question: {
    content: string;
    prompt: string;
    options: {
      value: string;
      label: string;
      description?: string;
      recommended?: boolean;
    }[];
  },
) {
  const rootId = `${roomId}:turn-${sequence}`;
  return roomEvent(roomId, sequence, 'room_post', {
    post: {
      schemaVersion: 'wisdom-weasel.room-post.v2',
      postId: `${roomId}:question-${sequence}`,
      roomId,
      rootId,
      generation: 0,
      dispatchId: `${roomId}:dispatch-${sequence}`,
      authorActorRef: `${roomId}:p1`,
      kind: 'wait',
      visibility: 'room',
      content: question.content,
      question: { prompt: question.prompt, options: question.options },
      idempotencyKey: `${roomId}:question-${sequence}`,
      publicationSource: { kind: 'room_commit', ref: `${roomId}:commit-${sequence}` },
      createdAtMs: sequence,
    },
  }, {
    turnId: rootId,
    participantId: `${roomId}:p1`,
    sourceSessionId: `${roomId}:s1`,
  });
}


function roomUserPostEvent(
  roomId: string,
  sequence: number,
  rootId: string,
  sourceRef: string,
  content: string,
) {
  return roomEvent(roomId, sequence, 'room_post', {
    post: {
      schemaVersion: 'wisdom-weasel.room-post.v2',
      postId: `${roomId}:user-post-${sequence}`,
      roomId,
      rootId,
      generation: 0,
      authorActorRef: 'user:local',
      kind: 'request',
      visibility: 'room',
      content,
      idempotencyKey: `user-message:${sourceRef}`,
      publicationSource: { kind: 'user', ref: sourceRef },
      createdAtMs: sequence,
    },
  }, {
    turnId: rootId,
    participantId: null,
    sourceSessionId: '',
  });
}

function roomTerminalPostEvent(
  roomId: string,
  sequence: number,
  kind: 'result' | 'handoff' | 'wait' | 'blocked',
  content: string,
) {
  const rootId = `${roomId}:turn-${sequence}`;
  return roomEvent(roomId, sequence, 'room_post', {
    post: {
      schemaVersion: 'wisdom-weasel.room-post.v2',
      postId: `${roomId}:${kind}-${sequence}`,
      roomId,
      rootId,
      generation: 0,
      dispatchId: `${roomId}:dispatch-${sequence}`,
      authorActorRef: `${roomId}:p1`,
      kind,
      visibility: 'room',
      content,
      ...(kind === 'handoff' ? { mentions: [`${roomId}:p2`] } : {}),
      idempotencyKey: `${roomId}:${kind}-${sequence}`,
      publicationSource: { kind: 'room_commit', ref: `${roomId}:commit-${sequence}` },
      createdAtMs: sequence,
    },
  }, {
    turnId: rootId,
    participantId: `${roomId}:p1`,
    sourceSessionId: `${roomId}:s1`,
  });
}

function roomEvent(
  roomId: string,
  sequence: number,
  eventType: string,
  payload: Record<string, unknown>,
  identity: {
    turnId?: string;
    participantId?: string | null;
    sourceSessionId?: string;
  } = {},
) {
  return {
    schemaVersion: 'rag-ime.agent-room-event.v1',
    eventId: `${roomId}:${sequence}`,
    roomId,
    sequence,
    turnId: identity.turnId ?? `${roomId}:turn-1`,
    eventType,
    participantId: identity.participantId ?? null,
    sourceSessionId: identity.sourceSessionId ?? '',
    createdAtMs: sequence,
    payload,
    resumeToken: `${roomId}:${sequence}`,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((accept, decline) => {
    resolve = accept;
    reject = decline;
  });
  return { promise, resolve, reject };
}
