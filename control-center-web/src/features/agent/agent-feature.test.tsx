import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { MockControlTransport } from '@/test/mock-transport';
import { StubControlTransport } from '@/test/stub-control-transport';
import { ControlTransportHttpError } from '@/platform/http-transport';
import type { ControlRequest, ControlTransport } from '@/platform/transport';
import { AgentFeature } from './index';
import { previewAgentEvents, previewAgentSnapshot, previewModelCatalog, previewPersonas, previewSessions, previewTemplates } from './preview-data';
import { resolveConversationEntryId } from './sessions/ConversationForkDialog';
import { SessionRail } from './sessions/SessionRail';
import { useAgentLiveStore } from './state/live-store';
import { groupToolActivities, projectStatusPanel } from './status/AgentStatusPanel';
import { AgentTurn } from './timeline/AgentTimeline';
import {
  sessionItems,
  type ModelCatalog,
  type SessionSummary,
  type ThinkingLevel,
} from './types';
import type { UiAgentMessage } from '@/contracts/ui-events';

const virtuosoMock = vi.hoisted(() => ({
  scrollToIndex: vi.fn(),
  atBottomStateChange: undefined as ((atBottom: boolean) => void) | undefined,
  components: undefined as Record<string, unknown> | undefined,
  followOutput: undefined as ((isAtBottom: boolean) => 'auto' | 'smooth' | false) | undefined,
  isScrolling: undefined as ((scrolling: boolean) => void) | undefined,
  scrollSeekConfiguration: undefined as unknown,
  scroller: undefined as HTMLDivElement | undefined,
}));

vi.mock('react-virtuoso', async () => {
  const React = await import('react');
  return {
    Virtuoso: React.forwardRef(({
    alignToBottom,
    atBottomStateChange,
    components,
    data,
    followOutput,
    initialTopMostItemIndex,
    isScrolling,
    itemContent,
    scrollSeekConfiguration,
    scrollerRef,
  }: {
    alignToBottom?: boolean;
    atBottomStateChange?: (atBottom: boolean) => void;
    components?: Record<string, unknown>;
    data: string[];
    followOutput?: (isAtBottom: boolean) => 'auto' | 'smooth' | false;
    initialTopMostItemIndex?: { index: string | number; align?: string };
    isScrolling?: (scrolling: boolean) => void;
    itemContent: (index: number, item: string) => ReactNode;
    scrollSeekConfiguration?: unknown;
    scrollerRef?: (scroller: HTMLElement | Window | null) => void;
  }, ref) => {
    const localScrollerRef = React.useRef<HTMLDivElement>(null);
    React.useImperativeHandle(ref, () => ({
      scrollToIndex: virtuosoMock.scrollToIndex,
    }));
    React.useLayoutEffect(() => {
      virtuosoMock.scroller = localScrollerRef.current ?? undefined;
      scrollerRef?.(localScrollerRef.current);
      return () => {
        scrollerRef?.(null);
        virtuosoMock.scroller = undefined;
      };
    }, [scrollerRef]);
    virtuosoMock.atBottomStateChange = atBottomStateChange;
    virtuosoMock.components = components;
    virtuosoMock.followOutput = followOutput;
    virtuosoMock.isScrolling = isScrolling;
    virtuosoMock.scrollSeekConfiguration = scrollSeekConfiguration;
    return (
      <div
        ref={localScrollerRef}
        data-align-to-bottom={alignToBottom || undefined}
        data-initial-align={initialTopMostItemIndex?.align}
        data-testid="agent-virtuoso"
      >
        {data.map((item, index) => <div key={item}>{itemContent(index, item)}</div>)}
      </div>
    );
  }),
  };
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  setDocumentVisibility('visible');
  virtuosoMock.scrollToIndex.mockReset();
  virtuosoMock.atBottomStateChange = undefined;
  virtuosoMock.components = undefined;
  virtuosoMock.followOutput = undefined;
  virtuosoMock.isScrolling = undefined;
  virtuosoMock.scrollSeekConfiguration = undefined;
  virtuosoMock.scroller = undefined;
  for (const session of previewSessions) useAgentLiveStore.getState().clear(session.id);
  useAgentLiveStore.getState().clear('session-history');
});

describe('Agent experience', () => {
  it('starts restoring a deep-linked conversation before the session rail finishes loading', async () => {
    const pendingSessions = deferred<unknown>();
    const pendingSnapshot = deferred<unknown>();
    const transport = featureTransport(
      undefined,
      undefined,
      () => pendingSessions.promise,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      () => pendingSnapshot.promise,
    );

    renderAgent(transport, '/agent?session=session-preview');

    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'agent.session.snapshot',
        params: { sessionId: 'session-preview' },
      }),
    })));
    expect(screen.getByRole('status', { name: '正在打开对话' })).toBeInTheDocument();
    expect(screen.getByText('先载入最近内容；完整记录可按需加载。')).toBeInTheDocument();

    pendingSessions.resolve({ ok: true, items: previewSessions });
    pendingSnapshot.resolve(previewAgentSnapshot('session-preview'));
    expect(await screen.findByRole('textbox', { name: '消息' })).toBeInTheDocument();
  });

  it('opens evaluation snapshots as frozen full transcripts without live controls', async () => {
    const evaluationSession = {
      ...previewSessions[0]!,
      id: 'session-evaluation',
      title: '冻结评测记录',
      evaluationSnapshot: true,
    };
    const transport = productionTransport({
      'agent.sessions.list': {
        ok: true,
        activeSessionId: evaluationSession.id,
        items: [evaluationSession],
      },
      'agent.session.snapshot': previewAgentSnapshot(evaluationSession.id),
    });

    renderAgent(transport, `/agent?session=${evaluationSession.id}`);

    expect(await screen.findByText('评测快照 · 只读证据')).toBeInTheDocument();
    expect(screen.getAllByText('读取输入法工具书，并把结果作为可展开卡片保留。').length).toBeGreaterThan(0);
    expect(screen.getByText('评测快照，只读证据')).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: '消息' })).not.toBeInTheDocument();
    expect(transport.subscriptionCount('agent.session.events')).toBe(0);
    expect(transport.requests.some((request) => (
      request.pathId === 'agent.runtime.get'
      || request.pathId === 'agent.session.models'
      || request.pathId === 'agent.session.commands'
      || request.pathId === 'agent.tools.list'
    ))).toBe(false);
    const snapshotRequests = transport.requests.filter((request) => (
      request.pathId === 'agent.session.snapshot'
    ));
    expect(snapshotRequests).toHaveLength(1);
    expect(snapshotRequests[0]).toEqual(expect.objectContaining({
      params: { sessionId: evaluationSession.id },
    }));
    expect(snapshotRequests[0]?.query).toBeUndefined();
  });

  it('renders and subscribes from recent history until the user requests the full record', async () => {
    const pendingFull = deferred<unknown>();
    const complete = previewAgentSnapshot('session-preview');
    const recentMessages = complete.messages.slice(-2);
    const transport = productionTransport({
      'agent.session.snapshot': (request: ControlRequest) => (
        request.query?.view === 'recent'
          ? {
              ...complete,
              items: recentMessages,
              messages: undefined,
              liveEvents: [],
              snapshotScope: 'recent',
              partial: true,
              recentFromSequence: 12,
            }
          : pendingFull.promise
      ),
    });

    const { container } = renderAgent(transport, '/agent?session=session-preview');

    expect(await screen.findByText(
      '读取输入法工具书，并把结果作为可展开卡片保留。',
    )).toBeInTheDocument();
    expect(screen.getByText('最近上下文')).toBeInTheDocument();
    const composer = screen.getByRole('textbox', { name: '消息' });
    expect(transport.requests.filter((request) => (
      request.pathId === 'agent.session.snapshot'
      && request.query?.view === 'recent'
    ))).toHaveLength(1);
    expect(transport.requests.filter((request) => (
      request.pathId === 'agent.session.snapshot'
      && request.query?.view === undefined
    ))).toHaveLength(0);
    expect(transport.subscriptionCount('agent.session.events')).toBe(1);

    await userEvent.setup().click(screen.getByRole('button', { name: '加载完整记录' }));
    await waitFor(() => expect(transport.requests.filter((request) => (
      request.pathId === 'agent.session.snapshot'
      && request.query?.view === undefined
    ))).toHaveLength(1));
    expect(screen.getByText('正在加载完整记录')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '消息' })).toBe(composer);

    await act(async () => pendingFull.resolve(complete));
    await waitFor(() => expect(
      useAgentLiveStore.getState().projections['session-preview']?.messageOrder,
    ).toHaveLength(4));
    expect(new Set(
      useAgentLiveStore.getState().projections['session-preview']?.messageOrder,
    ).size).toBe(4);
    expect(useAgentLiveStore.getState().projections['session-preview']?.turnOrder).toHaveLength(2);
    expect(container.querySelectorAll('.agent-turn')).toHaveLength(2);
    expect(screen.queryByText('最近上下文')).not.toBeInTheDocument();
  });

  it('opens and subscribes to a busy Session without starting the blocking full archive', async () => {
    const complete = previewAgentSnapshot('session-preview');
    const recentUser = complete.messages.at(-2)!;
    const transport = productionTransport({
      'agent.session.snapshot': (request: ControlRequest) => {
        if (request.query?.view !== 'recent') {
          throw new Error('busy first paint must not request the full archive');
        }
        return {
          ...complete,
          status: 'busy',
          items: [recentUser],
          messages: undefined,
          liveEvents: [],
          snapshotScope: 'recent',
          partial: true,
          recentFromSequence: 12,
        };
      },
    });

    renderAgent(transport, '/agent?session=session-preview');

    expect(await screen.findByText(
      '读取输入法工具书，并把结果作为可展开卡片保留。',
    )).toBeInTheDocument();
    expect(await screen.findByRole('textbox', { name: '消息' })).toBeEnabled();
    await waitFor(() => expect(
      transport.subscriptionCount('agent.session.events'),
    ).toBe(1));
    expect(transport.requests.filter((request) => (
      request.pathId === 'agent.session.snapshot'
      && request.query?.view === undefined
    ))).toHaveLength(0);
  });

  it('keeps a newer SSE turn when the background full archive resolves late', async () => {
    const pendingFull = deferred<unknown>();
    const complete = previewAgentSnapshot('session-preview');
    const recentMessages = complete.messages.slice(-2);
    const transport = productionTransport({
      'agent.session.snapshot': (request: ControlRequest) => (
        request.query?.view === 'recent'
          ? {
              ...complete,
              items: recentMessages,
              messages: undefined,
              liveEvents: [],
              snapshotScope: 'recent',
              partial: true,
              recentFromSequence: 12,
            }
          : pendingFull.promise
      ),
    });

    renderAgent(transport, '/agent?session=session-preview');

    await waitFor(() => expect(
      transport.subscriptionCount('agent.session.events'),
    ).toBe(1));
    await userEvent.setup().click(screen.getByRole('button', { name: '加载完整记录' }));
    const projection = useAgentLiveStore.getState().projections['session-preview'];
    const sequence = projection.lastSequence + 1;
    const latestMessage = complete.messages.at(-1) as UiAgentMessage;
    const streamedMessage = {
      ...latestMessage,
      id: 'session-preview:assistant-after-recent',
      turnId: 'session-preview:turn-after-recent',
      blocks: [{
        ...latestMessage.blocks[0]!,
        id: 'assistant-after-recent-text',
        data: { text: '这是 recent 之后通过 SSE 到达的新回复。' },
      }],
      createdAtMs: Date.now(),
      completedAtMs: Date.now(),
    };
    act(() => {
      transport.emit('agent.session.events', {
        schemaVersion: 'rag-ime.agent-event.v1',
        eventId: 'message-after-recent',
        sessionId: 'session-preview',
        turnId: streamedMessage.turnId,
        sequence,
        createdAtMs: Date.now(),
        streamKind: 'agent',
        eventType: 'message_completed',
        payload: { message: streamedMessage },
        resumeToken: `session-preview:${sequence}`,
      });
    });
    expect((await screen.findAllByText(
      '这是 recent 之后通过 SSE 到达的新回复。',
    )).length).toBeGreaterThan(0);

    await act(async () => pendingFull.resolve(complete));
    expect(screen.getAllByText('这是 recent 之后通过 SSE 到达的新回复。').length).toBeGreaterThan(0);
    expect(useAgentLiveStore.getState().projections['session-preview']?.lastSequence).toBe(sequence);
  });

  it('keeps an immediate optimistic send when the background full archive resolves late', async () => {
    const pendingFull = deferred<unknown>();
    const pendingPrompt = deferred<unknown>();
    const complete = previewAgentSnapshot('session-preview');
    const transport = productionTransport({
      'agent.session.prompt': () => pendingPrompt.promise,
      'agent.session.snapshot': (request: ControlRequest) => (
        request.query?.view === 'recent'
          ? {
              ...complete,
              items: complete.messages.slice(-2),
              messages: undefined,
              liveEvents: [],
              snapshotScope: 'recent',
              partial: true,
              recentFromSequence: 12,
            }
          : pendingFull.promise
      ),
    });
    const user = userEvent.setup();

    renderAgent(transport, '/agent?session=session-preview');
    await waitFor(() => expect(
      transport.subscriptionCount('agent.session.events'),
    ).toBe(1));
    await user.click(screen.getByRole('button', { name: '加载完整记录' }));
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await user.type(composer, 'full archive 不能覆盖这条刚发送的消息');
    await user.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(Object.keys(
      useAgentLiveStore.getState().projections['session-preview']?.optimisticByClientMessageId ?? {},
    )).toHaveLength(1));

    await act(async () => pendingFull.resolve(complete));
    expect(Object.keys(
      useAgentLiveStore.getState().projections['session-preview']?.optimisticByClientMessageId ?? {},
    )).toHaveLength(1);
    expect(screen.getAllByText('full archive 不能覆盖这条刚发送的消息').length).toBeGreaterThan(0);
    expect(screen.queryAllByText(
      '把迁移进度按真实代码链整理一下，别把工具日志当回答。',
    )).toHaveLength(0);
    expect(screen.getByText('最近上下文')).toBeInTheDocument();

    pendingPrompt.resolve({
      accepted: false,
      cancelled: true,
      abortRequested: true,
      admissionCancelled: true,
    });
  });

  it('does not present a settled recent user-only window as the completed conversation', async () => {
    const pendingFull = deferred<unknown>();
    const complete = previewAgentSnapshot('session-preview');
    const danglingUser = complete.messages.slice(2, 3);
    const transport = productionTransport({
      'agent.session.snapshot': (request: ControlRequest) => (
        request.query?.view === 'recent'
          ? {
              ...complete,
              items: danglingUser,
              messages: undefined,
              liveEvents: [],
              snapshotScope: 'recent',
              partial: true,
              recentFromSequence: 12,
            }
          : pendingFull.promise
      ),
    });

    renderAgent(transport, '/agent?session=session-preview');

    expect(await screen.findByText('最近上下文')).toBeInTheDocument();
    expect(screen.queryByText(
      '读取输入法工具书，并把结果作为可展开卡片保留。',
    )).not.toBeInTheDocument();
    expect(transport.requests.filter((request) => (
      request.pathId === 'agent.session.snapshot'
      && request.query?.view === undefined
    ))).toHaveLength(0);

    await userEvent.setup().click(screen.getByRole('button', { name: '加载完整记录' }));
    await act(async () => pendingFull.resolve(complete));
    expect((await screen.findAllByText(
      '读取输入法工具书，并把结果作为可展开卡片保留。',
    )).length).toBeGreaterThan(0);
    expect(screen.queryByText('最近上下文')).not.toBeInTheDocument();
  });

  it('keeps the Session usable and waits for an explicit full load when recent recovery fails', async () => {
    const complete = previewAgentSnapshot('session-preview');
    const transport = productionTransport({
      'agent.session.snapshot': (request: ControlRequest) => {
        if (request.query?.view === 'recent') throw new Error('recent unavailable');
        return complete;
      },
    });

    renderAgent(transport, '/agent?session=session-preview');

    expect(await screen.findByRole('textbox', { name: '消息' })).toBeEnabled();
    expect(await screen.findByText('最近上下文')).toBeInTheDocument();
    expect(transport.requests.filter((request) => (
      request.pathId === 'agent.session.snapshot'
      && request.query?.view === undefined
    ))).toHaveLength(0);

    await userEvent.setup().click(screen.getByRole('button', { name: '加载完整记录' }));
    await waitFor(() => expect(
      useAgentLiveStore.getState().projections['session-preview']?.messageOrder,
    ).toHaveLength(4));
  });

  it('makes the full session row clickable', async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<TooltipProvider><SessionRail sessions={previewSessions} selectedId="session-preview" loading={false} onSelect={onSelect} onCreate={() => {}} /></TooltipProvider>);
    expect(screen.getByText('personal-agent-workbench')).toBeInTheDocument();
    expect(screen.getByText('未指定项目')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '记忆整理' }));
    expect(onSelect).toHaveBeenCalledWith('session-memory');
  });

  it('collapses complete project groups and restores their conversations', async () => {
    const user = userEvent.setup();
    const { container, rerender } = render(
      <TooltipProvider>
        <SessionRail
          sessions={previewSessions}
          selectedId="session-preview"
          loading={false}
          onSelect={() => {}}
          onCreate={() => {}}
        />
      </TooltipProvider>,
    );

    const project = screen.getByRole('button', {
      name: /personal-agent-workbench/,
    });
    expect(project).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('button', { name: '控制中心迁移' })).toBeInTheDocument();

    await user.click(project);
    expect(project).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('button', { name: '控制中心迁移' })).not.toBeInTheDocument();
    const sessionGroup = container.querySelector<HTMLElement>('.agent-session-project__sessions');
    expect(sessionGroup).not.toBeNull();
    expect(getComputedStyle(sessionGroup!).display).toBe('none');

    rerender(
      <TooltipProvider>
        <SessionRail
          sessions={[...previewSessions]}
          selectedId="session-preview"
          loading={false}
          onSelect={() => {}}
          onCreate={() => {}}
        />
      </TooltipProvider>,
    );
    expect(project).toHaveAttribute('aria-expanded', 'false');
    expect(getComputedStyle(sessionGroup!).display).toBe('none');

    await user.click(project);
    expect(screen.getByRole('button', { name: '控制中心迁移' })).toBeInTheDocument();
    expect(getComputedStyle(sessionGroup!).display).toBe('grid');
  });

  it('offers archive, delete confirmation, and archived visibility controls per conversation', async () => {
    const onArchive = vi.fn();
    const onDelete = vi.fn();
    const onShowArchivedChange = vi.fn();
    const user = userEvent.setup();
    const { container } = render(
      <TooltipProvider>
        <SessionRail
          sessions={previewSessions}
          selectedId="session-preview"
          loading={false}
          onSelect={() => {}}
          onCreate={() => {}}
          onArchive={onArchive}
          onDelete={onDelete}
          onShowArchivedChange={onShowArchivedChange}
        />
      </TooltipProvider>,
    );
    const targetRow = [...container.querySelectorAll('.agent-session-row-shell')]
      .find((row) => row.textContent?.includes('记忆整理'));
    expect(targetRow).toBeDefined();

    expect(targetRow?.querySelector('.agent-session-row__archive')).toBeNull();
    expect(targetRow?.querySelectorAll('.agent-session-row__menu')).toHaveLength(1);
    expect(targetRow?.querySelector('.agent-session-row__copy small')).toHaveTextContent('已把最近输入整理为 3 个主题。');
    expect(targetRow?.querySelector('time')).not.toBeNull();
    await user.click(within(targetRow as HTMLElement).getByRole('button', { name: '更多“记忆整理”操作' }));
    const menuItems = await screen.findAllByRole('menuitem');
    expect(menuItems.map((item) => item.textContent)).toEqual(['归档对话', '删除对话']);
    await user.click(screen.getByRole('menuitem', { name: '归档对话' }));
    expect(onArchive).toHaveBeenCalledWith('session-memory', true);

    await user.click(within(targetRow as HTMLElement).getByRole('button', { name: '更多“记忆整理”操作' }));
    await user.click(await screen.findByRole('menuitem', { name: '删除对话' }));
    const dialog = await screen.findByRole('dialog', { name: /删除“记忆整理”/ });
    await user.click(within(dialog).getByRole('button', { name: '删除' }));
    expect(onDelete).toHaveBeenCalledWith('session-memory');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /删除“记忆整理”/ })).not.toBeInTheDocument());

    await user.click(screen.getByRole('button', { name: '对话列表选项' }));
    await user.click(await screen.findByRole('menuitemcheckbox', { name: '显示已归档对话' }));
    expect(onShowArchivedChange).toHaveBeenCalledWith(true);
  });

  it('keeps Room participant conversations out of the Session rail', () => {
    const ordinarySession = previewSessions.find((session) => session.id === 'session-memory')!;
    const roomSession = {
      ...ordinarySession,
      id: 'session-room-present',
      title: '迁移作战室 · 澄·今',
      roomParticipant: {
        roomId: 'room-preview',
        participantId: 'participant-present',
        status: 'active' as const,
      },
    };
    const onSelect = vi.fn();
    const { container } = render(
      <TooltipProvider>
        <SessionRail
          sessions={[ordinarySession, roomSession]}
          selectedId={ordinarySession.id}
          loading={false}
          onSelect={onSelect}
          onCreate={() => {}}
          onArchive={() => {}}
          onDelete={() => {}}
        />
      </TooltipProvider>,
    );
    const rows = [...container.querySelectorAll('.agent-session-row-shell')];
    const ordinaryRow = rows.find((row) => row.textContent?.includes(ordinarySession.title));
    const roomRow = rows.find((row) => row.textContent?.includes(roomSession.title));

    expect(rows).toHaveLength(1);
    expect(screen.getByText('1 段对话 · 0 个项目')).toBeInTheDocument();
    expect(ordinaryRow?.querySelectorAll('.agent-session-row__menu')).toHaveLength(1);
    expect(roomRow).toBeUndefined();
    expect(screen.queryByText(roomSession.title)).not.toBeInTheDocument();
    expect(onSelect).not.toHaveBeenCalled();
  });

  it('offers a PAWOS Session window from the conversation menu', async () => {
    const onOpenWindow = vi.fn();
    const user = userEvent.setup();
    render(
      <TooltipProvider>
        <SessionRail
          sessions={previewSessions}
          selectedId="session-preview"
          loading={false}
          onSelect={() => {}}
          onCreate={() => {}}
          onOpenWindow={onOpenWindow}
        />
      </TooltipProvider>,
    );

    await user.click(screen.getByRole('button', { name: '更多“记忆整理”操作' }));
    await user.click(await screen.findByRole('menuitem', { name: '独立窗口' }));

    expect(onOpenWindow).toHaveBeenCalledWith(expect.objectContaining({
      id: 'session-memory',
      title: '记忆整理',
    }));
  });

  it('offers restore from the same overflow menu for archived conversations', async () => {
    const onArchive = vi.fn();
    const user = userEvent.setup();
    render(
      <TooltipProvider>
        <SessionRail
          sessions={[{ ...previewSessions[1], status: 'archived' }]}
          selectedId="session-memory"
          loading={false}
          onSelect={() => {}}
          onCreate={() => {}}
          onArchive={onArchive}
        />
      </TooltipProvider>,
    );

    await user.click(screen.getByRole('button', { name: '更多“记忆整理”操作' }));
    await user.click(await screen.findByRole('menuitem', { name: '恢复对话' }));
    expect(onArchive).toHaveBeenCalledWith('session-memory', false);
  });

  it('keeps the delete confirmation open and shows the backend error when deletion fails', async () => {
    const onDelete = vi.fn().mockRejectedValue(new Error('DELETE requires application/json'));
    const user = userEvent.setup();
    const { container } = render(
      <TooltipProvider>
        <SessionRail
          sessions={previewSessions}
          selectedId="session-preview"
          loading={false}
          onSelect={() => {}}
          onCreate={() => {}}
          onDelete={onDelete}
        />
      </TooltipProvider>,
    );
    const targetRow = [...container.querySelectorAll('.agent-session-row-shell')]
      .find((row) => row.textContent?.includes('记忆整理'));

    await user.click(within(targetRow as HTMLElement).getByRole('button', { name: '更多“记忆整理”操作' }));
    await user.click(await screen.findByRole('menuitem', { name: '删除对话' }));
    const dialog = await screen.findByRole('dialog', { name: /删除“记忆整理”/ });
    await user.click(within(dialog).getByRole('button', { name: '删除' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('DELETE requires application/json');
    expect(dialog).toBeInTheDocument();
    await waitFor(() => expect(within(dialog).getByRole('button', { name: '删除' })).not.toBeDisabled());
  });

  it('resolves projected message ids to real Pi transcript entry ids', () => {
    expect(resolveConversationEntryId({
      items: [{
        entryId: 'pi-jsonl-entry-42',
        role: 'user',
        text: '修改之前的问题',
        createdAtMs: 1_234,
      }],
    }, [{
      entryId: 'projected-message-id',
      role: 'user',
      text: '修改之前的问题',
      createdAtMs: 1_234,
    }], 'projected-message-id')).toBe('pi-jsonl-entry-42');
  });

  it('renders a right-edge conversation navigator with turn previews', async () => {
    renderAgent(featureTransport());
    const navigator = await screen.findByRole('navigation', { name: '快速跳转对话' });
    const markers = within(navigator).getAllByRole('button');
    expect(markers.length).toBeGreaterThan(1);
    expect(markers[0]).toHaveTextContent('第 1 轮');
    expect(navigator).toHaveTextContent('读取输入法工具书');
    expect(navigator).toHaveTextContent('Agent');
  });

  it('keeps every mounted turn rendered and stops answering hover while the transcript scrolls', async () => {
    renderAgent(featureTransport());
    await screen.findByRole('textbox', { name: '消息' });
    const timeline = screen.getByRole('log', { name: '对话时间线' });

    // Scroll-seek swapped whole screens of conversation for empty measured
    // boxes above a velocity threshold and swapped them back on deceleration,
    // which is what made repeated up/down scrolling flicker.
    expect(virtuosoMock.scrollSeekConfiguration).toBeUndefined();
    expect(virtuosoMock.components).not.toHaveProperty('ScrollSeekPlaceholder');

    expect(timeline).not.toHaveAttribute('data-scrolling');
    act(() => virtuosoMock.isScrolling?.(true));
    expect(timeline).toHaveAttribute('data-scrolling', 'true');
    act(() => virtuosoMock.isScrolling?.(false));
    expect(timeline).not.toHaveAttribute('data-scrolling');
  });

  it('preserves bottom-follow through layout growth and restores it after the reader returns', async () => {
    const transport = featureTransport();
    renderAgent(transport);
    await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));
    const scroller = virtuosoMock.scroller!;
    Object.defineProperty(scroller, 'scrollHeight', { configurable: true, value: 900 });
    Object.defineProperty(scroller, 'clientHeight', { configurable: true, value: 400 });
    scroller.scrollTop = 100;
    act(() => virtuosoMock.atBottomStateChange?.(true));
    expect(virtuosoMock.followOutput?.(true)).toBe('auto');
    expect(virtuosoMock.followOutput?.(false)).toBe('auto');

    virtuosoMock.scrollToIndex.mockClear();
    const emitDelta = (eventId: string, delta: string) => {
      const current = useAgentLiveStore.getState().projections['session-preview'];
      useAgentLiveStore.getState().applyEvents('session-preview', [{
        schemaVersion: 'rag-ime.agent-event.v1',
        eventId,
        sessionId: 'session-preview',
        turnId: 'turn-stream-follow',
        sequence: current.lastSequence + 1,
        createdAtMs: Date.now(),
        streamKind: 'agent',
        eventType: 'text_delta',
        payload: { delta },
        resumeToken: `session-preview:${current.lastSequence + 1}`,
      }]);
    };

    act(() => emitDelta('stream-follow-1', 'first'));
    await waitFor(() => expect(scroller.scrollTop).toBe(900));

    scroller.scrollTop = 200;
    act(() => {
      scroller.dispatchEvent(new WheelEvent('wheel', { deltaY: -24 }));
    });
    expect(virtuosoMock.followOutput?.(false)).toBe(false);
    act(() => emitDelta('stream-follow-2', 'second'));
    await new Promise((resolve) => window.setTimeout(resolve, 20));
    expect(scroller.scrollTop).toBe(200);

    scroller.scrollTop = 500;
    act(() => scroller.dispatchEvent(new Event('scroll')));
    expect(virtuosoMock.followOutput?.(false)).toBe('auto');
    act(() => emitDelta('stream-follow-3', 'third'));
    await waitFor(() => expect(scroller.scrollTop).toBe(900));
    expect(virtuosoMock.scrollToIndex).not.toHaveBeenCalled();
  });

  it('coalesces overlapping gap markers into one authoritative recovery snapshot', async () => {
    const baseline = previewAgentSnapshot('session-preview');
    const recoverySnapshot = deferred<unknown>();
    let snapshotRequests = 0;
    const transport = featureTransport(
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      () => {
        snapshotRequests += 1;
        if (snapshotRequests === 1) return baseline;
        if (snapshotRequests === 2) return recoverySnapshot.promise;
        throw new Error(`unexpected snapshot request ${snapshotRequests}`);
      },
    );
    // Exercise the production hydration path rather than the mock preview
    // fixture branch while retaining the controllable in-memory transport.
    Object.defineProperty(transport, 'kind', { value: 'native' });
    renderAgent(transport);
    await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));

    const gapEvent = (sequence: number) => ({
      schemaVersion: 'rag-ime.agent-event.v1',
      eventId: `snapshot-required-${sequence}`,
      sessionId: 'session-preview',
      turnId: '',
      sequence,
      createdAtMs: sequence,
      streamKind: 'agent',
      eventType: 'snapshot_required',
      payload: { reason: 'event_replay_gap' },
      resumeToken: `session-preview:${sequence}`,
    });
    act(() => {
      transport.emit('agent.session.events', gapEvent(baseline.lastSequence + 1));
      transport.emit('agent.session.events', gapEvent(baseline.lastSequence + 2));
    });
    await waitFor(() => expect(snapshotRequests).toBe(2));

    const latestSequence = baseline.lastSequence + 20;
    await act(async () => {
      recoverySnapshot.resolve({
        ...baseline,
        lastSequence: latestSequence,
        resumeToken: `session-preview:${latestSequence}`,
      });
      await recoverySnapshot.promise;
    });
    await waitFor(() => {
      expect(useAgentLiveStore.getState().projections['session-preview'].resumeToken)
        .toBe(`session-preview:${latestSequence}`);
      expect(transport.subscriptionCalls.at(-1)?.request.lastEventId)
        .toBe(`session-preview:${latestSequence}`);
    });
    expect(snapshotRequests).toBe(2);
    expect(transport.subscriptionCalls).toHaveLength(2);
  });

  it('creates a real Pi-backed conversation branch and restores the selected message as draft', async () => {
    const transport = featureTransport();
    const user = userEvent.setup();
    renderAgent(transport);

    const branchButton = await screen.findByRole('button', { name: '查看对话路径与分支' }, { timeout: 5_000 });
    await waitFor(() => expect(branchButton).toBeEnabled());
    await user.click(branchButton);
    const dialog = await screen.findByRole('dialog', { name: '对话路径' });
    expect(within(dialog).getByText(/所有公开消息都可跳转和创建分支/)).toBeInTheDocument();
    expect(within(dialog).queryByText(/private deep-search evidence/)).not.toBeInTheDocument();
    await user.click(await within(dialog).findByRole('radio', { name: /读取输入法工具书，并把结果作为可展开卡片保留/ }));
    await user.click(within(dialog).getByRole('button', { name: '创建分支' }));

    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'agent.session.forks.create',
        params: { sessionId: 'session-preview' },
        body: expect.objectContaining({ entryId: 'session-preview:user-media' }),
      }),
    })));
    expect(await screen.findByRole('textbox', { name: '消息' })).toHaveValue('读取输入法工具书，并把结果作为可展开卡片保留。');
    expect(screen.getByRole('button', { name: '控制中心迁移 · 分支' })).toHaveAttribute('aria-current', 'true');
    await waitFor(() => expect(
      transport.subscriptionCalls.filter((call) => (
        call.request.pathId === 'agent.session.events'
        && call.request.params?.sessionId === 'session-forked'
      )),
    ).toHaveLength(1));
  });

  it('creates a branch directly from the selected user message', async () => {
    const transport = featureTransport();
    const user = userEvent.setup();
    renderAgent(transport);

    const actions = await screen.findAllByRole('button', { name: '从这条消息创建分支' }, { timeout: 5_000 });
    const action = actions.find((item) => (
      item.closest('.agent-user-message-shell')?.getAttribute('data-agent-message-id')
        === 'session-preview:user-media'
    ));
    expect(action).toBeDefined();
    await user.click(action!);
    const dialog = await screen.findByRole('dialog', { name: '对话路径' });
    const create = within(dialog).getByRole('button', { name: '创建分支' });
    await waitFor(() => expect(create).toBeEnabled());
    await user.click(create);

    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'agent.session.forks.create',
        params: { sessionId: 'session-preview' },
        body: expect.objectContaining({ entryId: 'session-preview:user-media' }),
      }),
    })));
    expect(await screen.findByRole('textbox', { name: '消息' })).toHaveValue('读取输入法工具书，并把结果作为可展开卡片保留。');
  });

  it('does not warm the fork catalog while the selected Session is busy', async () => {
    const forkListRoute = vi.fn(() => forkListFixture());
    const transport = productionTransport({
      'agent.session.snapshot': {
        ...previewAgentSnapshot('session-preview'),
        status: 'busy',
        messages: [],
        liveEvents: [],
      },
      'agent.session.forks.list': forkListRoute,
      'agent.runtime.get': {
        schemaVersion: 'rag-ime.agent-runtime.v1',
        enabled: true,
        managed: true,
        status: 'ready',
        capabilities: { conversationFork: true, conversationRewrite: true },
      },
    });

    renderAgent(transport);

    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      pathId: 'agent.session.snapshot',
    })));
    await waitFor(() => expect(
      useAgentLiveStore.getState().projections['session-preview']?.status,
    ).toBe('busy'));
    expect(forkListRoute).not.toHaveBeenCalled();
    expect(transport.requests).not.toContainEqual(expect.objectContaining({
      pathId: 'agent.session.forks.list',
    }));
  });

  it('opens historical editing immediately while Pi resolves the branch anchor in the background', async () => {
    const pendingForkCatalog = deferred<ReturnType<typeof forkListFixture>>();
    const transport = featureTransport(
      undefined, // model catalog
      undefined, // tool catalog
      undefined, // sessions
      undefined, // prompt
      undefined, // subagent
      undefined, // approval
      undefined, // abort
      undefined, // runtime
      undefined, // snapshot
      undefined, // fork create
      undefined, // model select
      undefined, // thinking select
      undefined, // mode update
      () => pendingForkCatalog.promise,
    );
    const user = userEvent.setup();
    renderAgent(transport);

    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({ pathId: 'agent.session.forks.list' }),
    })));
    const actions = await screen.findAllByRole('button', { name: '修改这条消息' });
    const action = actions.find((item) => (
      item.closest('.agent-user-message-shell')?.getAttribute('data-agent-message-id')
        === 'session-preview:user-media'
    ));
    await user.click(action!);

    expect(await screen.findByText('正在修改这条消息', {}, { timeout: 500 })).toBeInTheDocument();
    expect(screen.getByText('正在定位历史锚点；内容现在就可以编辑')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '消息' })).toHaveValue(
      '读取输入法工具书，并把结果作为可展开卡片保留。',
    );
    expect(screen.getByRole('button', { name: /^发送/ })).toBeDisabled();

    pendingForkCatalog.resolve(forkListFixture());
    expect(await screen.findByText('发送后将从这里重新生成后续对话')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^发送/ })).toBeEnabled();
  });

  it('rewinds the same conversation when double Escape edits the previous user message', async () => {
    const transport = featureTransport();
    const user = userEvent.setup();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(screen.getAllByRole('button', { name: '修改这条消息' }).length).toBeGreaterThan(0));

    composer.focus();
    await user.keyboard('{Escape}{Escape}');
    expect(await screen.findByText('正在修改这条消息')).toBeInTheDocument();
    expect(composer).toHaveValue('读取输入法工具书，并把结果作为可展开卡片保留。');
    await user.clear(composer);
    await user.type(composer, '改成更准确的问题');
    await user.click(screen.getByRole('button', { name: '发送' }));

    // The visible branch rewinds synchronously; restoring Pi and admitting the
    // replacement prompt may still finish later in the background.
    expect(useAgentLiveStore.getState().projections['session-preview']
      .messagesById['session-preview:assistant-media']).toBeUndefined();
    expect(Object.values(useAgentLiveStore.getState().projections['session-preview'].messagesById)
      .some((message) => message.id.startsWith('local:web-rewrite-'))).toBe(true);

    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'agent.session.rewrite',
        params: { sessionId: 'session-preview' },
        body: expect.objectContaining({
          entryId: 'session-preview:user-media',
          message: '改成更准确的问题',
        }),
      }),
    })));
    expect(screen.queryByRole('button', { name: '打开命令面板' })).not.toBeInTheDocument();
  });

  it('keeps a replayed Pi prompt singular and edits through its authoritative transcript anchor', async () => {
    const snapshot = previewAgentSnapshot('session-preview');
    const canonical = snapshot.messages.find((message) => (
      typeof message === 'object'
      && message !== null
      && 'id' in message
      && message.id === 'session-preview:user-media'
    )) as UiAgentMessage | undefined;
    expect(canonical).toBeDefined();
    const prompt = '读取输入法工具书，并把结果作为可展开卡片保留。';
    const replayTurnId = 'turn-rewrite-replay';
    const replayMessage = {
      ...canonical!,
      id: 'event:user-media',
      turnId: replayTurnId,
      clientMessageId: 'web-rewrite-replay',
      createdAtMs: canonical!.createdAtMs + 400,
      completedAtMs: (canonical!.completedAtMs ?? canonical!.createdAtMs) + 400,
    };
    const transport = featureTransport(
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      {
        ...snapshot,
        lastSequence: 13,
        resumeToken: 'session-preview:13',
        liveEvents: [{
          schemaVersion: 'rag-ime.agent-event.v1',
          eventId: 'event:rewrite-replay',
          sessionId: 'session-preview',
          turnId: replayTurnId,
          sequence: 13,
          createdAtMs: replayMessage.createdAtMs,
          eventType: 'message_completed',
          payload: { clientMessageId: replayMessage.clientMessageId, message: replayMessage },
          resumeToken: 'session-preview:13',
        }],
      },
    );
    const user = userEvent.setup();
    const { container } = renderAgent(transport);

    await screen.findAllByText(prompt, { exact: true });
    let messageShell: HTMLElement | undefined;
    await waitFor(() => {
      const matchingShells = [...container.querySelectorAll<HTMLElement>('.agent-user-message-shell')]
        .filter((item) => item.textContent?.includes(prompt));
      expect(matchingShells).toHaveLength(1);
      [messageShell] = matchingShells;
    });
    await user.click(within(messageShell!).getByRole('button', { name: '修改这条消息' }));

    expect(await screen.findByText('正在修改这条消息')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '消息' })).toHaveValue(prompt);
    expect(screen.queryByText('Pi 没有返回这条公开消息对应的可回溯锚点。')).not.toBeInTheDocument();
  });

  it('creates a branch after an assistant message and keeps the new composer empty', async () => {
    const forkCreate = (request: { body?: unknown }) => {
      const body = request.body as { entryId?: string };
      return {
        schemaVersion: 'rag-ime.agent-session-fork-create.v1',
        ok: true,
        sourceSessionId: 'session-preview',
        entryId: body.entryId ?? '',
        selectedText: '',
        session: {
          ...previewSessions[0],
          schemaVersion: 'rag-ime.agent-session.v1',
          id: 'session-forked-assistant',
          title: '回答之后 · 分支',
          createdAtMs: Date.now() - 1_000,
          messageCount: 4,
          updatedAtMs: Date.now(),
        },
      };
    };
    const transport = featureTransport(
      undefined, undefined, undefined, undefined, undefined, undefined,
      undefined, undefined, undefined, forkCreate,
    );
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole('button', { name: '查看对话路径与分支' }));
    const dialog = await screen.findByRole('dialog', { name: '对话路径' });
    const assistant = await within(dialog).findByRole('radio', { name: /已完成。正文展示工具书内容/ });
    await user.click(assistant);
    expect(within(assistant).getByText('可跳转 · 可分支')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: '跳到节点' })).toBeEnabled();
    expect(within(dialog).getByRole('button', { name: '创建分支' })).toBeEnabled();
    await user.click(within(dialog).getByRole('button', { name: '创建分支' }));

    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'agent.session.forks.create',
        params: { sessionId: 'session-preview' },
        body: expect.objectContaining({ entryId: 'session-preview:assistant-media' }),
      }),
    })));
    expect(await screen.findByRole('textbox', { name: '消息' })).toHaveValue('');
  });

  it('lists every conversation node and jumps back to an assistant message', async () => {
    const user = userEvent.setup();
    renderAgent(featureTransport());

    await user.click(await screen.findByRole('button', { name: '查看对话路径与分支' }));
    const dialog = await screen.findByRole('dialog', { name: '对话路径' });
    await user.click(within(dialog).getByRole('radio', { name: /已完成。正文展示工具书内容/ }));
    await user.click(within(dialog).getByRole('button', { name: '跳到节点' }));

    await waitFor(() => expect(document.querySelector(
      '[data-agent-message-id="session-preview:assistant-media"][data-history-target="true"]',
    )).not.toBeNull());
    expect(screen.queryByRole('dialog', { name: '对话路径' })).not.toBeInTheDocument();
  });

  it('exposes native fork capability without waiting for slower session catalogs', async () => {
    let resolveTools!: (value: unknown) => void;
    const slowTools = new Promise<unknown>((resolve) => { resolveTools = resolve; });
    renderAgent(featureTransport(undefined, () => slowTools));

    const branchButton = await screen.findByRole('button', { name: '查看对话路径与分支' });
    await waitFor(() => expect(branchButton).toBeEnabled());
    resolveTools({ ok: true, items: toolCatalog() });
  });

  it('does not offer branches when the active Pi protocol declares them unsupported', async () => {
    const transport = featureTransport(
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      {
        schemaVersion: 'rag-ime.agent-runtime.v1',
        enabled: true,
        managed: true,
        status: 'ready',
        piVersion: '0.80.7',
        idleTimeoutSeconds: 600,
        capabilities: { conversationFork: false },
      },
    );
    renderAgent(transport);

    const navigator = await screen.findByRole('button', { name: '查看对话路径与分支' });
    await waitFor(() => expect(navigator).toBeEnabled());
    fireEvent.click(navigator);
    expect(await screen.findByText('当前运行时未提供分支能力，历史节点仍可直接跳转。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '创建分支' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: '从这条消息创建分支' })).not.toBeInTheDocument();
  });

  it('keeps delegated runtime sessions out of the user conversation rail', () => {
    const parent = { ...previewSessions[0]!, id: 'session-parent', title: '用户主对话' };
    const child = {
      ...parent,
      id: 'session-child-runtime',
      title: '研究员临时会话',
      sessionKind: 'subagent_runtime',
    };

    expect(sessionItems({ ok: true, items: [parent, child] })).toEqual([parent]);
  });

  it('keeps App-owned Sessions out of Agent while allowing their owning App to project them', () => {
    const agent = { ...previewSessions[0]!, id: 'session-agent', title: '用户主对话' };
    const appOwned = {
      ...agent,
      id: 'session-app-owned',
      title: '掌柜问数 · 问数',
      surfaceKind: 'extension_app' as const,
      ownerAppId: 'extension:zhanggui-wenshu',
      surfaceKey: 'ask',
    };
    const memoryOwned = {
      ...agent,
      id: 'session-memory-owned',
      title: 'Memory 管家 · 2026-08-31',
      surfaceKind: 'builtin_app' as never,
      ownerAppId: 'memory',
      surfaceKey: 'journal-2026-08-31',
    };

    expect(sessionItems({ ok: true, items: [agent, appOwned, memoryOwned] })).toEqual([agent]);
    expect(sessionItems(
      { ok: true, items: [agent, appOwned, memoryOwned] },
      { includeAppOwned: true },
    )).toEqual([agent, appOwned, memoryOwned]);
  });

  it('preserves Room member conversation metadata for task-view deep links', () => {
    const roomMember = {
      ...previewSessions[0]!,
      id: 'session-room-member',
      title: '联调 Room · 澄',
      sessionKind: 'conversation',
      roomParticipant: {
        roomId: 'room:feature-test',
        participantId: 'participant:feature-test',
        status: 'active' as const,
      },
    };

    expect(
      sessionItems({ ok: true, items: [roomMember] })
    ).toEqual([roomMember]);
  });

  it('does not request ordinary forks or offer history mutation for a Room participant', async () => {
    const roomSession = {
      ...previewSessions[0]!,
      id: 'session-preview',
      title: '联调 Room · 澄',
      roomParticipant: {
        roomId: 'room:feature-test',
        participantId: 'participant:feature-test',
        status: 'active' as const,
      },
    };
    const transport = featureTransport(
      undefined,
      undefined,
      { ok: true, items: [roomSession] },
    );
    const user = userEvent.setup();
    renderAgent(transport, '/agent?session=session-preview');

    expect(await screen.findAllByText('联调 Room · 澄')).toHaveLength(1);
    expect(screen.getByText('0 段对话 · 0 个项目')).toBeInTheDocument();
    expect(document.querySelector('.room-task-flow')).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: '任务依赖图' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '更多“联调 Room · 澄”操作' })).not.toBeInTheDocument();
    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({ pathId: 'agent.session.snapshot' }),
    })));
    expect(transport.requests).not.toContainEqual(expect.objectContaining({
      request: expect.objectContaining({ pathId: 'agent.session.forks.list' }),
    }));
    expect(screen.queryByRole('button', { name: '修改这条消息' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '查看对话路径与分支' }));
    const dialog = await screen.findByRole('dialog', { name: '对话路径' });
    expect(dialog).toHaveTextContent(
      '这段对话属于 Room participant，历史分支与修改由 Room 管理。',
    );
    expect(within(dialog).getByRole('button', { name: '创建分支' })).toBeDisabled();
    expect(transport.requests).not.toContainEqual(expect.objectContaining({
      request: expect.objectContaining({ pathId: 'agent.session.forks.list' }),
    }));
  });

  it('renders one activity container per assistant turn without avatar columns or raw payloads', async () => {
    const sessionId = 'session-preview';
    const snapshot = previewAgentSnapshot(sessionId);
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, snapshot);
    const events = previewAgentEvents(sessionId);
    events[1] = { ...events[1]!, payload: { ...events[1]!.payload, rawSecret: '{"token":"do-not-render"}' } };
    useAgentLiveStore.getState().applyEvents(sessionId, events);
    const turnId = `${sessionId}:turn-media`;
    render(<TooltipProvider><AgentTurn sessionId={sessionId} turnId={turnId} persona={previewPersonas[0]} onApprovalDecision={() => {}} /></TooltipProvider>);
    expect(screen.queryByRole('img', { name: 'Pi Agent' })).not.toBeInTheDocument();
    expect(document.querySelector('.agent-persona-avatar')).not.toBeInTheDocument();
    expect(document.querySelectorAll('.agent-activity')).toHaveLength(1);
    expect(document.querySelector('.agent-user-message')).toBeInTheDocument();
    expect(screen.queryByText(/do-not-render/)).not.toBeInTheDocument();
    expect(document.querySelector('.agent-assistant-message')).toHaveTextContent('正文展示工具书内容');
  });

  it('replays lifecycle cancellation owner receipts in the on-demand status panel', async () => {
    const snapshot = {
      ...previewAgentSnapshot('session-preview'),
      lifecycleCancellationAudits: [{
        schemaVersion: 'rag-ime.agent-lifecycle-cancellation-audit.v1',
        requestId: 'lifecycle:goal:pause:1',
        sessionId: 'session-preview',
        scopeKind: 'goal',
        scopeId: 'goal:session-preview',
        sourceRevision: 2,
        transitionRevision: 3,
        action: 'pause',
        reason: '用户暂停当前目标',
        state: 'partial',
        sourceTurnId: 'session-preview:turn-1',
        owners: {
          runtime: { status: 'succeeded', receipt: { lifecycle: 'aborted' } },
          approval: { status: 'succeeded', receipt: { cancelledCount: 1 } },
          job: { status: 'partial', receipt: { cancelled: 1, stillRunning: 1 } },
          delegation: { status: 'excluded', receipt: { reason: 'no_active_delegation' } },
        },
        createdAtMs: 100,
        updatedAtMs: 200,
      }],
    };
    const transport = productionTransport({ 'agent.session.snapshot': snapshot });
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole('button', { name: '展开任务中心' }));
    const statusPanel = await screen.findByLabelText('当前对话任务中心');

    expect(await within(statusPanel).findByRole('button', { name: /取消与暂停回执/ })).toHaveAttribute('aria-expanded', 'true');
    expect(within(statusPanel).getByText('目标暂停')).toBeVisible();
    expect(within(statusPanel).getByText('用户暂停当前目标')).toBeVisible();
    expect(within(statusPanel).getByText('部分完成')).toBeVisible();
    expect(within(statusPanel).getByText('lifecycle: aborted')).toBeVisible();
    expect(within(statusPanel).getByText('stillRunning: 1', { exact: false })).toBeVisible();
  });

  it('keeps a failed subagent query distinct from empty and retries only its Session owner', async () => {
    let attempts = 0;
    const transport = productionTransport({
      'agent.subagents.list': () => {
        attempts += 1;
        if (attempts === 1) throw new Error('subagent runtime offline');
        return { ok: true, items: [] };
      },
    });
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole('button', { name: '展开任务中心' }));
    const statusPanel = await screen.findByLabelText('当前对话任务中心');
    const sectionToggle = await within(statusPanel).findByRole('button', { name: /子 Agent 运行树/ });
    const section = sectionToggle.closest('section')!;

    expect(await within(section).findByRole('alert')).toHaveTextContent('子智能体状态读取失败');
    expect(within(section).queryByText('当前会话没有委派任务')).not.toBeInTheDocument();
    const retry = within(section).getByRole('button', { name: '重新读取子智能体' });
    expect(retry).toBeEnabled();
    await user.click(retry);

    await waitFor(() => expect(attempts).toBe(2));
    expect(within(section).queryByRole('alert')).not.toBeInTheDocument();
    expect(within(section).getByText('当前会话没有委派任务')).toBeVisible();
  });

  it('does not poll subagents from an open task center in a background tab', async () => {
    setDocumentVisibility('hidden');
    let attempts = 0;
    const transport = productionTransport({
      'agent.subagents.list': () => {
        attempts += 1;
        return { ok: true, items: [] };
      },
    });
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole('button', { name: '展开任务中心' }));
    expect(await screen.findByLabelText('当前对话任务中心')).toBeInTheDocument();
    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 20));
    });
    expect(attempts).toBe(0);

    setDocumentVisibility('visible');
    fireEvent(document, new Event('visibilitychange'));
    await waitFor(() => expect(attempts).toBe(1));
  });

  it('shows persisted message usage when an idle snapshot has no live context telemetry', async () => {
    const base = previewAgentSnapshot('session-preview');
    let assistantIndex = 0;
    const usages = [
      { input: 1_501, output: 208, cacheRead: 8_704, cacheWrite: 0, totalTokens: 10_413 },
      { input: 563, output: 219, cacheRead: 61_952, cacheWrite: 0, totalTokens: 62_734 },
    ];
    const snapshot = {
      ...base,
      telemetry: undefined,
      messages: (base.messages as UiAgentMessage[]).map((message) => {
        if (message.role !== 'assistant') return message;
        const usage = usages[assistantIndex] ?? usages.at(-1)!;
        assistantIndex += 1;
        return {
          ...message,
          provider: 'openai-codex',
          model: 'gpt-5.6-luna',
          usage,
        };
      }),
    };
    const transport = productionTransport({
      'agent.session.snapshot': snapshot,
    });
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole('button', { name: '展开任务中心' }));
    const statusPanel = await screen.findByLabelText('当前对话任务中心');
    const telemetryToggle = await within(statusPanel).findByRole('button', { name: /上下文与用量/ });
    // Full task-center sections are expanded by default. Keep this assertion
    // aligned with the disclosure contract instead of toggling a visible
    // section closed before checking its persisted-message fallback.
    expect(telemetryToggle).toHaveAttribute('aria-expanded', 'true');

    const section = telemetryToggle.closest('section')!;
    expect(within(section).getByText('可见回合用量')).toBeInTheDocument();
    expect(within(section).getByLabelText('已持久化回合 Token 用量')).toHaveTextContent('输入72.7K');
    expect(within(section).getByText('当前 Provider 上下文精确占用未保存')).toBeInTheDocument();
    expect(within(section).queryByText('发送一轮消息后显示上下文与缓存数据')).not.toBeInTheDocument();
  });

  it('shows the full capability catalog while keeping Runtime authorization and native Skills distinct', async () => {
    const enabled = { ...toolCatalog().find((tool) => tool.id === 'input')!, enabled: true };
    const disabled = { ...toolCatalog().find((tool) => tool.id === 'knowledge')!, enabled: false };
    const transport = productionTransport({
      'agent.tools.list': capabilityToolCatalog([enabled, disabled]),
    });
    const user = userEvent.setup();
    renderAgent(transport);
    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      pathId: 'agent.tools.list',
      query: { sessionId: 'session-preview' },
    })));

    await user.click(await screen.findByRole('button', { name: '展开任务中心' }));
    const statusPanel = await screen.findByLabelText('当前对话任务中心');
    const capabilityToggle = await within(statusPanel).findByRole('button', { name: /当前对话工具与技能/ });
    expect(capabilityToggle).toHaveTextContent('可用能力目录');
    expect(capabilityToggle).toHaveTextContent('未注入上下文');
    const capabilitySection = capabilityToggle.closest('section')!;
    await user.click(capabilityToggle);
    await user.click(within(capabilitySection).getByRole('button', { name: '管理当前对话的工具与技能' }));

    const dialog = screen.getByRole('dialog', { name: '管理当前对话的工具与技能' });
    expect(within(dialog).getByText('browser')).toBeInTheDocument();
    expect(within(dialog).getByText('输入法')).toBeInTheDocument();
    expect(within(dialog).getByText('知识检索')).toBeInTheDocument();
    for (const disclosure of within(dialog).getAllByText('查看来源、权限和生效依据')) {
      await user.click(disclosure);
    }
    expect(within(dialog).getByText(/Pi native Skills/)).toBeInTheDocument();
    expect(within(dialog).getAllByText('已由现有策略授权')).toHaveLength(2);
    expect(within(dialog).getByText('当前未获执行授权')).toBeInTheDocument();
    expect(within(dialog).getAllByRole('combobox')).toHaveLength(3);
  });
  it('shows the installed backend capability version mismatch and retries without adapting the legacy list', async () => {
    const transport = productionTransport({
      'agent.tools.list': {
        schemaVersion: 'rag-ime.control-tool-list.v1',
        ok: true,
        items: [{ id: 'legacy-input', displayName: '旧输入工具' }],
      },
    });
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole('button', { name: '展开任务中心' }));
    const statusPanel = await screen.findByLabelText('当前对话任务中心');
    const capabilityToggle = await within(statusPanel).findByRole('button', { name: /当前对话工具与技能/ });
    const capabilitySection = capabilityToggle.closest('section')!;
    await user.click(capabilityToggle);
    expect(within(capabilitySection).getByRole('alert')).toHaveTextContent('服务返回 rag-ime.control-tool-list.v1');
    expect(within(capabilitySection).queryByText('旧输入工具')).not.toBeInTheDocument();

    const before = transport.requests.filter((call) => call.pathId === 'agent.tools.list').length;
    await user.click(within(capabilitySection).getByRole('button', { name: '重试' }));
    await waitFor(() => expect(transport.requests.filter((call) => call.pathId === 'agent.tools.list')).toHaveLength(before + 1));
  });


  it('opens the memory control from a historical receipt deep link without changing preferences', async () => {
    const transport = productionTransport();
    renderAgent(transport, '/agent?session=session-preview&tools=memory');
    const search = await screen.findByRole('textbox', { name: '搜索工具' });
    expect(search).toHaveValue('记忆');
    expect(transport.requests.some((call) => call.pathId === 'agent.session.capability-policy.update')).toBe(false);
  });

  it('persists a temporary capability preference and renders the backend-confirmed outcome', async () => {
    let disabled = false;
    const transport = productionTransport({
      'agent.session.capability-policy.update': () => {
        disabled = true;
        return { ok: true };
      },
      'agent.tools.list': (request: ControlRequest) => {
        const ownerSessionId = typeof request.query?.sessionId === 'string' ? request.query.sessionId : 'session-preview';
        const catalog = capabilityToolCatalog(toolCatalog(), ownerSessionId);
        if (!disabled) return catalog;
        return {
          ...catalog,
          sessionPolicy: {
            ...catalog.sessionPolicy,
            disclosurePreferences: {
              ...catalog.sessionPolicy.disclosurePreferences,
              session: { 'tool:input': 'disabled' },
              effective: {
                ...catalog.sessionPolicy.disclosurePreferences.effective,
                'tool:input': 'disabled',
              },
            },
          },
          items: catalog.items.map((item) => item.canonicalId === 'tool:input' ? {
            ...item,
            disclosure: {
              preference: 'disabled',
              effective: 'disabled',
              state: 'hidden',
              reason: 'session_preference',
            },
            effectiveScope: 'session',
            reasons: ['session_preference'],
          } : item),
        };
      },
    });
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole('button', { name: '展开任务中心' }));
    const statusPanel = await screen.findByLabelText('当前对话任务中心');
    const capabilityToggle = await within(statusPanel).findByRole('button', { name: /当前对话工具与技能/ });
    const capabilitySection = capabilityToggle.closest('section')!;
    await user.click(capabilityToggle);
    await user.click(within(capabilitySection).getByRole('button', { name: '管理当前对话的工具与技能' }));
    await user.click(screen.getByRole('combobox', { name: '输入法的当前对话临时设置' }));
    await user.click(await screen.findByRole('option', { name: '关闭' }));

    await waitFor(() => expect(transport.requests).toContainEqual({
      pathId: 'agent.session.capability-policy.update',
      params: { sessionId: 'session-preview' },
      body: {
        capabilityDisclosurePreferences: {
          'tool:input': 'disabled',
        },
      },
    }));
    expect(await screen.findByText('当前对话临时设置已保存')).toBeVisible();
    expect(screen.getByText(/后端已确认隐藏/)).toBeVisible();
    expect(screen.getByRole('combobox', { name: '输入法的当前对话临时设置' })).toHaveTextContent('关闭');
  });

  it('keeps a failed temporary override on its Session owner and retries the exact preference', async () => {
    let attempts = 0;
    const transport = productionTransport({
      'agent.session.capability-policy.update': () => {
        attempts += 1;
        if (attempts === 1) throw new Error('临时策略修订冲突');
        return { ok: true };
      },
    });
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole('button', { name: '展开任务中心' }));
    const statusPanel = await screen.findByLabelText('当前对话任务中心');
    const capabilityToggle = await within(statusPanel).findByRole('button', { name: /当前对话工具与技能/ });
    const capabilitySection = capabilityToggle.closest('section')!;
    await user.click(capabilityToggle);
    await user.click(within(capabilitySection).getByRole('button', { name: '管理当前对话的工具与技能' }));
    await user.click(screen.getByRole('combobox', { name: '输入法的当前对话临时设置' }));
    await user.click(await screen.findByRole('option', { name: '关闭' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('临时策略修订冲突');

    await user.click(screen.getByRole('button', { name: '重试这项调整' }));
    await waitFor(() => expect(attempts).toBe(2));
    const updates = transport.requests.filter((call) => call.pathId === 'agent.session.capability-policy.update');
    expect(updates).toHaveLength(2);
    expect(updates[1]?.body).toEqual({
      capabilityDisclosurePreferences: { 'tool:input': 'disabled' },
    });
    expect(await screen.findByText('当前对话临时设置已保存')).toBeVisible();
  });

  it('keeps an existing Runtime job visible and cancellable after hiding workspace_job from the next turn', async () => {
    let jobDisclosureDisabled = false;
    const runningJob = {
      schemaVersion: 'rag-ime.agent-background-job.v1',
      jobId: 'bg_0123456789abcdef0123456789abcdef',
      sessionId: 'session-preview',
      label: '构建工作区索引',
      status: 'running',
      command: 'pnpm build:index',
      commandSha256: 'a'.repeat(64),
      cwd: '/workspace',
      networkAllowed: false,
      maxRunSeconds: 120,
      pid: 42,
      createdAtMs: 100,
      startedAtMs: 110,
      updatedAtMs: 120,
      endedAtMs: 0,
      exitCode: null,
      outputBytes: 0,
      logStartCursor: 0,
      logTruncated: false,
      cancelRequestedAtMs: 0,
      error: '',
      approvalId: 'approval-1',
      causalMetadata: {
        todoId: 'todo-1',
        todoRevision: 1,
        goalId: 'goal-1',
        goalRevision: 1,
        turnId: 'turn-1',
        roomBound: false,
      },
    } as const;
    const cancellingJob = {
      ...runningJob,
      status: 'cancelling',
      updatedAtMs: 200,
      cancelRequestedAtMs: 200,
      error: 'control_center_requested',
    } as const;
    const transport = productionTransport({
      'agent.session.snapshot': {
        ...previewAgentSnapshot('session-preview'),
        backgroundJobs: [runningJob],
      },
      'agent.tools.list': (request: ControlRequest) => {
        const ownerSessionId = typeof request.query?.sessionId === 'string' ? request.query.sessionId : 'session-preview';
        const catalog = capabilityToolCatalog(toolCatalog(), ownerSessionId);
        if (!jobDisclosureDisabled) return catalog;
        return {
          ...catalog,
          sessionPolicy: {
            ...catalog.sessionPolicy,
            disclosurePreferences: {
              ...catalog.sessionPolicy.disclosurePreferences,
              session: { 'tool:workspace_job': 'disabled' },
              effective: {
                ...catalog.sessionPolicy.disclosurePreferences.effective,
                'tool:workspace_job': 'disabled',
              },
            },
          },
          items: catalog.items.map((item) => item.canonicalId === 'tool:workspace_job' ? {
            ...item,
            disclosure: {
              preference: 'disabled',
              effective: 'disabled',
              state: 'hidden',
              reason: 'session_preference',
            },
            effectiveScope: 'session',
            reasons: ['session_preference'],
          } : item),
        };
      },
      'agent.session.capability-policy.update': () => {
        jobDisclosureDisabled = true;
        return { ok: true };
      },
      'agent.session.backgroundJobs.list': {
        schemaVersion: 'rag-ime.agent-background-job-list.v1',
        ok: true,
        sessionId: 'session-preview',
        items: [runningJob],
        activeCount: 1,
      },
      'agent.session.backgroundJob.logs': {
        schemaVersion: 'rag-ime.agent-background-job-log.v1',
        ok: true,
        jobId: runningJob.jobId,
        sessionId: 'session-preview',
        cursor: 0,
        nextCursor: 0,
        logStartCursor: 0,
        truncatedBeforeCursor: false,
        hasMore: false,
        text: '',
      },
      'agent.session.backgroundJob.cancel': {
        schemaVersion: 'rag-ime.agent-background-job-cancel-receipt.v1',
        ok: true,
        summary: '已请求停止后台任务',
        alreadyTerminal: false,
        job: cancellingJob,
        cancelReceipt: {
          jobId: runningJob.jobId,
          requestedAtMs: 200,
          status: 'cancelling',
        },
      },
    });
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole('button', { name: '展开任务中心' }));
    const statusPanel = await screen.findByLabelText('当前对话任务中心');
    await within(statusPanel).findByRole('button', { name: /构建工作区索引/ });
    const capabilityToggle = await within(statusPanel).findByRole('button', { name: /当前对话工具与技能/ });
    const capabilitySection = capabilityToggle.closest('section')!;
    await user.click(capabilityToggle);
    await user.click(within(capabilitySection).getByRole('button', { name: '管理当前对话的工具与技能' }));
    await user.click(screen.getByRole('combobox', { name: '后台任务的当前对话临时设置' }));
    await user.click(await screen.findByRole('option', { name: '关闭' }));

    expect(await screen.findByText('当前对话临时设置已保存')).toBeVisible();
    await user.keyboard('{Escape}');
    const preservedJobButton = within(statusPanel).getByRole('button', { name: /构建工作区索引/ });
    expect(preservedJobButton).toBeVisible();
    await user.click(preservedJobButton);
    await user.click(within(statusPanel).getByRole('button', { name: '停止任务' }));
    await user.click(within(statusPanel).getByRole('button', { name: '确认停止' }));
    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      pathId: 'agent.session.backgroundJob.cancel',
      params: { sessionId: 'session-preview', jobId: runningJob.jobId },
      body: { reason: 'control_center_requested' },
    })));
  });

  it('keeps live agent, knowledge-source, and ephemeral subagent status visible without adding child Sessions', async () => {
    vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
      matches: query === '(min-width: 1180px)',
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })));
    const parentSession = { ...previewSessions[0]!, id: 'session-preview', title: '研究主对话' };
    const transport = featureTransport(
      undefined,
      undefined,
      { ok: true, items: [parentSession] },
      undefined,
      subagentListFixture(),
    );
    const user = userEvent.setup();
    renderAgent(transport);

    expect(await screen.findByText('1 段对话 · 1 个项目')).toBeInTheDocument();
    expect(document.querySelectorAll('.agent-session-row')).toHaveLength(1);
    expect(screen.queryByText('研究员临时会话')).not.toBeInTheDocument();

    const statusPanel = await screen.findByLabelText('当前对话任务中心');
    expect(statusPanel).toHaveAttribute('data-open', 'true');
    await waitFor(() => expect(
      statusPanel.querySelector('.agent-status-subagent strong')
    ).toHaveTextContent('研究员'));
    const subagentTree = statusPanel.querySelector<HTMLElement>('.agent-status-subagents');
    expect(subagentTree).toBeInTheDocument();
    if (!subagentTree) throw new Error('expected visible subagent run tree');
    const todoPanel = await within(statusPanel).findByRole('region', { name: 'Todo' });
    expect(todoPanel).toHaveTextContent('1/3 已收束');
    expect(todoPanel).toHaveTextContent('实现会话内可见的 Todo');
    expect(within(statusPanel).getAllByText('当前回合 · 已完成')).toHaveLength(2);
    expect(within(statusPanel).queryByText('执行中 · 1/3')).not.toBeInTheDocument();
    expect(within(subagentTree).getByText('Pattern 子调用 · 规划员')).toBeInTheDocument();
    expect(within(subagentTree).getByLabelText('核对记忆设计与来源 的子调用')).toBeInTheDocument();
    expect(within(subagentTree).getByText('审阅者')).toBeInTheDocument();
    expect(within(subagentTree).getByText('执行者')).toBeInTheDocument();
    expect(within(subagentTree).getAllByText('关联 Todo：实现 · 接入前端')).toHaveLength(4);
    expect(statusPanel.querySelector('.agent-status-subagent[data-state="running"] .agent-status-subagent__state svg')).toBeInTheDocument();
    expect(statusPanel.querySelector('.agent-status-subagent[data-state="queued"] .agent-status-subagent__state svg')).toBeInTheDocument();
    expect(statusPanel.querySelector('.agent-status-subagent[data-state="completed"]')).toBeInTheDocument();
    expect(statusPanel.querySelector('.agent-status-subagent[data-state="failed"]')).toBeInTheDocument();
    expect(statusPanel.querySelector('.agent-status-subagent[data-state="failed"]')).toHaveTextContent('public failure');
    expect(statusPanel.querySelector('.agent-status-subagent[data-state="running"] time')).toHaveTextContent(/^\d+(?:分\d{2})?秒$/);
    expect(within(subagentTree).getAllByRole('button', { name: '查看进度' })).toHaveLength(2);
    expect(within(subagentTree).getAllByRole('button', { name: '查看结果' })).toHaveLength(2);

    await waitFor(() => expect(useAgentLiveStore.getState().projections['session-preview']?.lastSequence).toBeGreaterThan(0));
    const projection = useAgentLiveStore.getState().projections['session-preview'];
    act(() => {
      useAgentLiveStore.getState().applyEvents('session-preview', [{
        schemaVersion: 'rag-ime.agent-event.v1',
        eventId: 'knowledge-live-started',
        sessionId: 'session-preview',
        turnId: 'turn-knowledge-live',
        sequence: projection.lastSequence + 1,
        createdAtMs: Date.now(),
        streamKind: 'agent',
        eventType: 'tool_started',
        payload: {
          toolCallId: 'knowledge-live',
          toolId: 'knowledge',
          operation: 'find',
          summary: '正在检索可引用的记忆证据',
          partialResult: {
            items: [
              { fileName: 'memory-design.md', citation: { startLine: 42, endLine: 48 } },
              { fileName: 'agent-runtime.pdf', citation: { page: 7 } },
            ],
          },
        },
        resumeToken: 'knowledge-live-started',
      }]);
    });

    const knowledgeStep = await within(statusPanel).findByText('知识库');
    expect(statusPanel.querySelector('.agent-status-turn[data-state="running"] > svg')).toBeInTheDocument();
    expect(statusPanel.querySelector('.agent-status-tool[data-state="running"] .agent-status-tool__icon svg')).toBeInTheDocument();
    await user.click(knowledgeStep);
    const knowledgeTool = knowledgeStep.closest('.agent-status-tool') as HTMLElement;
    expect(within(knowledgeTool).getByText('knowledge')).toBeInTheDocument();
    expect(within(knowledgeTool).getByText('find')).toBeInTheDocument();
    expect(within(knowledgeTool).getByText('0 个字段')).toBeInTheDocument();
    expect(within(knowledgeTool).getByText('来源 2 · 进行中')).toBeInTheDocument();
    expect(within(knowledgeTool).getByText('信息来源')).toBeInTheDocument();
    expect(within(knowledgeTool).getByText('memory-design.md · 42-48 行')).toBeInTheDocument();
    expect(within(knowledgeTool).getByText('agent-runtime.pdf · 第 7 页')).toBeInTheDocument();
  });

  it('shows a compact selectable Session run tree with one-node detail and explicit return direction', async () => {
    vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
      matches: query === '(min-width: 1180px)',
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })));
    const parentSession = { ...previewSessions[0]!, id: 'session-preview', title: '研究主对话' };
    const transport = featureTransport(
      undefined,
      undefined,
      { ok: true, items: [parentSession] },
      undefined,
      subagentListFixture(),
    );
    const user = userEvent.setup();
    renderAgent(transport);

    await waitFor(() => expect(
      transport.requests.some((call) => call.request.pathId === 'agent.session.commands'),
    ).toBe(true));
    await user.click(await screen.findByRole('button', { name: '打开子 Agent 工作台' }));
    const workspace = await screen.findByLabelText('Session 子 Agent 工作台');
    expect(workspace).toHaveAttribute('data-open', 'true');
    const graph = within(workspace).getByRole('region', { name: '子 Agent 运行图' });
    expect(within(graph).getByRole('button', { name: /研究员.*核对记忆设计与来源/u })).toBeVisible();
    const planner = within(graph).getByRole('button', { name: /规划员.*整理实现顺序/u });
    expect(planner).toBeVisible();
    expect(graph.querySelectorAll('.session-subagent-tree__edge').length).toBeGreaterThan(0);
    const detail = within(workspace).getByRole('region', { name: '子 Agent 节点详情' });
    expect(within(detail).getByText('核对记忆设计与来源')).toBeVisible();
    expect(within(detail).getByText('返回给 Root')).toBeVisible();
    await user.click(planner);
    expect(within(detail).getByText('整理实现顺序')).toBeVisible();
    expect(within(detail).getByText('由研究员派发')).toBeVisible();
    expect(within(detail).getByText(/返回给\s*研究员/u)).toBeVisible();
    expect(within(workspace).getByRole('link', { name: '打开子 Agent 设置' }))
      .toHaveAttribute('href', '#/configuration?section=subagents');
    expect(within(workspace).getByText('配置新的子 Agent')).toBeVisible();
  });

  it('keeps every tool step from the current turn available in the status panel', () => {
    const sessionId = 'session-tool-audit';
    useAgentLiveStore.getState().appendOptimistic(sessionId, {
      clientMessageId: 'tool-audit',
      text: '检查所有步骤',
      nowMs: 1,
    });
    const projection = useAgentLiveStore.getState().projections[sessionId];
    const turnId = projection.turnOrder[0]!;
    useAgentLiveStore.getState().applyEvents(sessionId, Array.from({ length: 12 }, (_, index) => ({
      schemaVersion: 'rag-ime.agent-event.v1' as const,
      eventId: `tool-step-${index}`,
      sessionId,
      turnId,
      sequence: index + 1,
      createdAtMs: index + 2,
      streamKind: 'agent' as const,
      eventType: 'tool_started' as const,
      payload: {
        toolCallId: `tool-call-${index}`,
        toolId: 'knowledge',
        operation: 'search',
        summary: `检索步骤 ${index + 1}`,
      },
      resumeToken: `tool-step-${index}`,
    })));

    expect(projectStatusPanel(useAgentLiveStore.getState().projections[sessionId]).tools).toHaveLength(12);
    useAgentLiveStore.getState().clear(sessionId);
  });

  it('groups consecutive agents contract corrections without hiding the receipts', () => {
    const activities = [
      'tasks cannot be combined with single-task delegation fields',
      'Validation failed: root must not have additional properties',
      'todoTask is required when delegating from a Session with Todo tasks',
      'todoTask must be the current in_progress Todo task',
    ].map((error, index) => ({
      id: `agents-contract-${index}`,
      kind: 'tool_failed',
      status: 'failed',
      payload: { toolId: 'agents', operation: 'delegate', error },
    })) as unknown as Parameters<typeof groupToolActivities>[0];

    const groups = groupToolActivities(activities);
    expect(groups).toHaveLength(1);
    expect(groups[0]?.kind).toBe('attempts');
    expect(groups[0]?.activities).toHaveLength(4);
  });

  it('keeps the durable Session Todo out of current-turn progress', () => {
    const sessionId = 'session-todo-panel';
    useAgentLiveStore.getState().appendOptimistic(sessionId, {
      clientMessageId: 'todo-panel',
      text: '按 Todo 执行',
      nowMs: 1,
    });
    const projection = useAgentLiveStore.getState().projections[sessionId];
    const turnId = projection.turnOrder[0]!;
    const todo = {
      schemaVersion: 'rag-ime.agent-todo.v1',
      id: `todo:${sessionId}`,
      sessionId,
      revision: 2,
      actor: 'agent',
      updatedAtMs: 3,
      phases: [{
        name: '实现',
        tasks: [
          { content: '核对权限边界', status: 'completed' },
          { content: '验证原生交互', status: 'in_progress' },
        ],
      }],
      counts: { total: 2, pending: 0, inProgress: 1, completed: 1, abandoned: 0 },
    };
    useAgentLiveStore.getState().applyEvents(sessionId, [{
      schemaVersion: 'rag-ime.agent-event.v1',
      eventId: 'todo-start',
      sessionId,
      turnId,
      sequence: 1,
      createdAtMs: 2,
      streamKind: 'agent',
      eventType: 'tool_started',
      payload: {
        toolCallId: 'todo-call',
        toolId: 'todo',
        operation: 'view',
        summary: '正在读取 Todo',
      },
      resumeToken: 'todo-start',
    }, {
      schemaVersion: 'rag-ime.agent-event.v1',
      eventId: 'todo-result',
      sessionId,
      turnId,
      sequence: 2,
      createdAtMs: 3,
      streamKind: 'agent',
      eventType: 'tool_finished',
      payload: {
        toolCallId: 'todo-call',
        toolId: 'todo',
        operation: 'view',
        summary: 'Todo 已读取',
        result: { details: { result: { todo } } },
      },
      resumeToken: 'todo-result',
    }]);

    const current = useAgentLiveStore.getState().projections[sessionId];
    expect(projectStatusPanel(current).tasks).toEqual([]);
    expect(current.todo.phases[0]?.tasks.map((item) => item.content)).toEqual([
      '核对权限边界',
      '验证原生交互',
    ]);
    useAgentLiveStore.getState().clear(sessionId);
  });

  it('keeps hydrated legacy history replies beside their user turns', () => {
    const sessionId = 'session-history';
    useAgentLiveStore.getState().hydrate(sessionId, {
      lastSequence: 4,
      resumeToken: `${sessionId}:4`,
      items: [
        historyMessage(sessionId, 'assistant-orphan', 'assistant', '上一段回复'),
        historyMessage(sessionId, 'user-one', 'user', '第一个问题'),
        historyMessage(sessionId, 'assistant-one-a', 'assistant', '第一段回复'),
        historyMessage(sessionId, 'assistant-one-b', 'assistant', '第一段补充'),
        historyMessage(sessionId, 'user-two', 'user', '第二个问题'),
        historyMessage(sessionId, 'user-three', 'user', '第三个问题'),
        historyMessage(sessionId, 'assistant-three', 'assistant', '第三段回复'),
      ],
    });

    const projection = useAgentLiveStore.getState().projections[sessionId];
    expect(projection.turnOrder).toEqual([
      'history:assistant-orphan',
      'history:user-one',
      'history:user-two',
      'history:user-three',
    ]);
    expect(projection.turnsById['history:user-one'].messageIds).toEqual([
      'user-one',
      'assistant-one-a',
      'assistant-one-b',
    ]);
    expect(projection.turnsById['history:user-one'].status).toBe('completed');
    expect(projection.turnsById['history:user-two'].messageIds).toEqual(['user-two']);

    render(
      <TooltipProvider>
        <AgentTurn
          sessionId={sessionId}
          turnId="history:user-three"
          persona={previewPersonas[0]}
          onApprovalDecision={() => {}}
        />
      </TooltipProvider>,
    );
    expect(screen.getByText('第三个问题')).toBeInTheDocument();
    expect(screen.getByText('第三段回复')).toBeInTheDocument();
  });

  it('sends through the allowlisted prompt path', async () => {
    const transport = featureTransport();
    const user = userEvent.setup();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await user.type(composer, '检查 reducer 边界');
    await user.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.session.prompt')).toBe(true));
    const prompt = transport.requests.find((call) => call.request.pathId === 'agent.session.prompt');
    expect(prompt?.request.params).toEqual({ sessionId: 'session-preview' });
    expect(prompt?.request.body).toMatchObject({ message: '检查 reducer 边界' });
    expect(prompt?.request.body).not.toHaveProperty('delivery');
  });

  it('sends Enter as a native follow-up without restoring a permanent delivery switch', async () => {
    const transport = featureTransport();
    const user = userEvent.setup();
    renderAgent(transport);

    const composer = await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(
      useAgentLiveStore.getState().projections['session-preview']?.messageOrder.length,
    ).toBeGreaterThan(0));
    act(() => useAgentLiveStore.getState().appendOptimistic('session-preview', {
      clientMessageId: 'native-queue-active',
      text: '正在处理当前任务',
      nowMs: Date.now(),
    }));
    expect(screen.queryByRole('radiogroup', { name: '消息投递方式' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '干预当前执行' })).not.toBeInTheDocument();
    await user.type(composer, '完成后再整理测试结果');
    await user.keyboard('{Enter}');

    await waitFor(() => expect(
      transport.requests.filter((call) => call.request.pathId === 'agent.session.prompt'),
    ).toHaveLength(1));
    const prompts = transport.requests.filter((call) => call.request.pathId === 'agent.session.prompt');
    expect(prompts[0]?.request.body).toMatchObject({
      message: '完成后再整理测试结果',
      delivery: 'followUp',
    });
    expect(screen.getByText('已接收，等待当前执行完成')).toBeInTheDocument();
    expect(transport.requests.some((call) => call.request.pathId === 'agent.session.abort')).toBe(false);
  });

  it('renders a bordered assistant processing surface immediately without an avatar placeholder', async () => {
    const pendingPrompt = new Promise(() => {});
    const transport = featureTransport(
      previewModelCatalog('session-preview'),
      { ok: true, items: toolCatalog() },
      { ok: true, items: previewSessions },
      () => pendingPrompt,
    );
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });

    fireEvent.change(composer, { target: { value: '立刻显示处理状态' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    const queuedTurn = Object.values(useAgentLiveStore.getState().projections['session-preview'].turnsById)
      .find((turn) => turn.id.startsWith('local-turn:'));
    expect(queuedTurn?.status).toBe('queued');
    const pending = await screen.findByRole('status');
    expect(pending).toHaveClass('agent-assistant-pending');
    expect(pending).toHaveTextContent('思考中');
    expect(pending).toHaveTextContent('0秒');
    expect(pending).toHaveTextContent('消息已收到');
    const assistantTurn = pending.closest('.agent-assistant-turn');
    expect(assistantTurn).not.toBeNull();
    expect(within(assistantTurn as HTMLElement).queryByRole('img', { name: 'Pi Agent' })).not.toBeInTheDocument();
    expect(assistantTurn?.querySelector('.agent-persona-avatar')).not.toBeInTheDocument();
    expect(within(assistantTurn as HTMLElement).getByText('Agent')).toBeInTheDocument();
    expect(within(assistantTurn as HTMLElement).getByText('正在处理')).toBeInTheDocument();
    expect(transport.requests.some((call) => call.request.pathId === 'agent.session.prompt')).toBe(true);
  });

  it('deduplicates rapid sends and isolates a late prompt failure to its source Session', async () => {
    const pendingA = deferred<unknown>();
    const transport = featureTransport(
      previewModelCatalog('session-preview'),
      { ok: true, items: toolCatalog() },
      { ok: true, items: previewSessions },
      (request: ControlRequest) => request.params?.sessionId === 'session-preview'
        ? pendingA.promise
        : { ok: true },
    );
    const user = userEvent.setup();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });

    fireEvent.change(composer, { target: { value: 'A 会迟到失败' } });
    const send = screen.getByRole('button', { name: '发送' });
    fireEvent.click(send);
    fireEvent.click(send);

    await waitFor(() => expect(
      transport.requests.filter((call) => call.request.pathId === 'agent.session.prompt'),
    ).toHaveLength(1));

    await user.click(screen.getByRole('button', { name: '记忆整理' }));
    await waitFor(() => expect(screen.getByRole('textbox', { name: '消息' })).toHaveValue(''));
    await user.type(screen.getByRole('textbox', { name: '消息' }), 'B 可以独立发送');
    await user.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(
      transport.requests.filter((call) => call.request.pathId === 'agent.session.prompt'),
    ).toHaveLength(2));
    expect(transport.requests.filter((call) => call.request.pathId === 'agent.session.prompt')[1]?.request)
      .toMatchObject({ params: { sessionId: 'session-memory' }, body: { message: 'B 可以独立发送' } });

    await user.type(screen.getByRole('textbox', { name: '消息' }), 'B 的未发送草稿');
    await act(async () => pendingA.reject(new Error('404 Model "gpt-5.6-luna" is not supported by any configured account in this group')));
    expect(screen.getByRole('textbox', { name: '消息' })).toHaveValue('B 的未发送草稿');
    expect(document.querySelector('.agent-conversation__header [role="alert"]')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '控制中心迁移' }));
    await waitFor(() => expect(screen.getByRole('textbox', { name: '消息' })).toHaveValue('A 会迟到失败'));
    expect(Object.values(useAgentLiveStore.getState().projections['session-preview'].turnsById)
      .some((turn) => turn.failure === '当前模型不可用，请切换模型后重试。')).toBe(true);
  });

  it('does not replace a newer draft when an earlier prompt fails late in the same Session', async () => {
    const pendingPrompt = deferred<unknown>();
    const transport = featureTransport(
      previewModelCatalog('session-preview'),
      { ok: true, items: toolCatalog() },
      { ok: true, items: previewSessions },
      () => pendingPrompt.promise,
    );
    const user = userEvent.setup();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });

    await user.type(composer, '先发送的消息');
    await user.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(composer).toHaveValue(''));
    await user.type(composer, '发送期间写下的新草稿');

    await act(async () => pendingPrompt.reject(new Error('late provider failure')));

    expect(composer).toHaveValue('发送期间写下的新草稿');
    const projection = useAgentLiveStore.getState().projections['session-preview'];
    expect(Object.values(projection.turnsById).some((turn) => (
      turn.status === 'failed' && Boolean(turn.failure)
    ))).toBe(true);
  });

  it('keeps an in-flight stop request and its late failure inside the source Session', async () => {
    const pendingAbort = deferred<unknown>();
    const transport = featureTransport(
      previewModelCatalog('session-preview'),
      { ok: true, items: toolCatalog() },
      { ok: true, items: previewSessions },
      { ok: true },
      { ok: true, items: [] },
      { ok: true },
      () => pendingAbort.promise,
    );
    renderAgent(transport);
    await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(
      useAgentLiveStore.getState().projections['session-preview']?.messageOrder.length,
    ).toBeGreaterThan(0));

    // Seed B before Stop starts so the ownership assertion does not spend the
    // product's real 1.45s reconciliation budget waiting for a Session switch
    // or synthetic typing while the full suite is under load.
    fireEvent.click(screen.getByRole('button', { name: '记忆整理' }));
    await waitFor(() => expect(
      screen.getByRole('button', { name: '记忆整理' }),
    ).toHaveAttribute('aria-current', 'true'));
    fireEvent.change(screen.getByRole('textbox', { name: '消息' }), {
      target: { value: 'B 不应被 A 的停止请求锁住' },
    });
    fireEvent.click(screen.getByRole('button', { name: '控制中心迁移' }));
    await waitFor(() => expect(
      screen.getByRole('button', { name: '控制中心迁移' }),
    ).toHaveAttribute('aria-current', 'true'));

    act(() => useAgentLiveStore.getState().appendOptimistic('session-preview', {
      clientMessageId: 'stop-source-active',
      text: 'A 正在处理',
      nowMs: Date.now(),
    }));

    fireEvent.click(await screen.findByRole('button', { name: '停止本轮' }));
    await waitFor(() => expect(
      transport.requests.filter((call) => call.request.pathId === 'agent.session.abort'),
    ).toHaveLength(1));

    fireEvent.click(screen.getByRole('button', { name: '记忆整理' }));
    await act(async () => pendingAbort.reject(new Error('A 的停止请求失败')));
    await waitFor(() => expect(
      screen.getByRole('button', { name: '记忆整理' }),
    ).toHaveAttribute('aria-current', 'true'));
    const composerB = screen.getByRole('textbox', { name: '消息' });
    expect(composerB).toHaveValue('B 不应被 A 的停止请求锁住');
    expect(screen.getByRole('button', { name: '发送' })).toBeEnabled();
    expect(screen.queryByText('A 的停止请求失败')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '控制中心迁移' }));
    expect(await screen.findByText('A 的停止请求失败')).toBeInTheDocument();
  }, 30_000);

  it('renders a rejected Pi prompt once with a public recovery message', async () => {
    const transport = featureTransport(
      previewModelCatalog('session-preview'),
      { ok: true, items: toolCatalog() },
      { ok: true, items: previewSessions },
      () => { throw new Error('404 Model "gpt-5.6-luna" is not supported by any configured account in this group'); },
    );
    const user = userEvent.setup();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });

    await user.type(composer, '你是谁');
    await user.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.session.prompt')).toBe(true));
    const projection = useAgentLiveStore.getState().projections['session-preview'];
    const failedTurn = Object.values(projection.turnsById).find((turn) => turn.failure);
    expect(failedTurn?.failure).toBe('当前模型不可用，请切换模型后重试。');
    expect(document.querySelector('.agent-conversation__header [role="alert"]')).not.toBeInTheDocument();
    expect(screen.queryByText(/not supported by any configured account/i)).not.toBeInTheDocument();
  });

  it('reports a native route version mismatch instead of blaming the model', async () => {
    const transport = featureTransport(
      previewModelCatalog('session-preview'),
      { ok: true, items: toolCatalog() },
      { ok: true, items: previewSessions },
      () => { throw new Error('Unexpected request body field: delivery'); },
    );
    const user = userEvent.setup();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });

    await user.type(composer, '检查原生路由版本');
    await user.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.session.prompt')).toBe(true));
    const projection = useAgentLiveStore.getState().projections['session-preview'];
    const failedTurn = Object.values(projection.turnsById).find((turn) => turn.failure);
    expect(failedTurn?.failure).toBe('控制中心组件版本不一致，请更新并重新打开控制中心。');
  });

  it('retries a failed turn through the real prompt route with the original input', async () => {
    let attempt = 0;
    const transport = featureTransport(
      previewModelCatalog('session-preview'),
      { ok: true, items: toolCatalog() },
      { ok: true, items: previewSessions },
      () => {
        attempt += 1;
        if (attempt === 1) throw new Error('model unavailable');
        return new Promise(() => {});
      },
    );
    const user = userEvent.setup();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await user.type(composer, '重试时保留这句话');
    await user.click(screen.getByRole('button', { name: '发送' }));

    const retry = await screen.findByRole('button', { name: '重试本轮' });
    await user.click(retry);

    const retryProjection = useAgentLiveStore.getState().projections['session-preview'];
    const queuedRetry = Object.values(retryProjection.turnsById)
      .find((turn) => turn.id.startsWith('local-turn:') && turn.status === 'queued');
    expect(queuedRetry).toBeDefined();
    expect(
      queuedRetry?.messageIds
        .map((messageId) => retryProjection.messagesById[messageId])
        .some((message) => message?.blocks.some(
          (block) => block.data.text === '重试时保留这句话',
        )),
    ).toBe(true);
    await waitFor(() => expect(transport.requests.filter((call) => call.request.pathId === 'agent.session.prompt')).toHaveLength(2));
    const timeline = screen.getByRole('log', { name: '对话时间线' });
    await waitFor(() => expect(
      [...timeline.querySelectorAll<HTMLElement>('.agent-user-message')]
        .filter((message) => message.textContent?.includes('重试时保留这句话')),
    ).toHaveLength(1));
    expect(
      [...timeline.querySelectorAll<HTMLElement>('.agent-turn')]
        .filter((turn) => turn.textContent?.includes('重试时保留这句话')),
    ).toHaveLength(1);
    expect(screen.queryByRole('button', { name: '重试本轮' })).not.toBeInTheDocument();
    const prompts = transport.requests.filter((call) => call.request.pathId === 'agent.session.prompt');
    expect(prompts[1]?.request.params).toEqual({ sessionId: 'session-preview' });
    expect(prompts[1]?.request.body).toMatchObject({ message: '重试时保留这句话', attachments: [] });
    const firstClientMessageId = String(
      (prompts[0]?.request.body as Record<string, unknown>)
        .clientMessageId,
    );
    const retryBody = prompts[1]?.request.body as Record<string, unknown>;
    expect(retryBody.clientMessageId).not.toBe(firstClientMessageId);
    expect(retryBody.retryOfClientMessageId).toBe(firstClientMessageId);
  });
  it('rolls back the classic retry card when admission becomes unresolved before submission', async () => {
    const pendingSessionRefresh = deferred<unknown>();
    let holdSessionRefresh = false;
    const transport = featureTransport(
      previewModelCatalog('session-preview'),
      { ok: true, items: toolCatalog() },
      () => holdSessionRefresh
        ? pendingSessionRefresh.promise
        : { ok: true, items: previewSessions },
      () => {
        throw new Error('model unavailable');
      },
    );
    const user = userEvent.setup();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await user.type(composer, '这条消息需要安全重试');
    await user.click(screen.getByRole('button', { name: '发送' }));

    const retry = await screen.findByRole('button', { name: '重试本轮' });
    const projection = useAgentLiveStore.getState().projections['session-preview']!;
    const userMessage = Object.values(projection.messagesById).find((message) => (
      message.role === 'user'
      && message.blocks.some((block) => block.data.text === '这条消息需要安全重试')
    ))!;
    const clientMessageId = userMessage.clientMessageId;
    expect(clientMessageId).toEqual(expect.any(String));
    const sessionListRequestsBeforeRetry = transport.requests.filter(
      ({ request }) => request.pathId === 'agent.sessions.list',
    ).length;
    holdSessionRefresh = true;
    await user.click(retry);
    await waitFor(() => expect(transport.requests.filter(
      ({ request }) => request.pathId === 'agent.sessions.list',
    )).toHaveLength(sessionListRequestsBeforeRetry + 1));

    act(() => {
      useAgentLiveStore.getState().failOptimistic(
        'session-preview',
        clientMessageId!,
        'receipt unresolved',
        Date.now(),
        'unresolved',
      );
      pendingSessionRefresh.resolve({ ok: true, items: previewSessions });
    });
    expect(await screen.findByText('这条消息仍无法确认是否已执行；为避免重复执行，不能自动重试。请刷新对话检查结果后，再决定是否发送新的请求。')).toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.prompt')).toHaveLength(1);

    act(() => {
      const store = useAgentLiveStore.getState();
      store.requeueOptimistic('session-preview', clientMessageId!, Date.now());
      store.failOptimistic('session-preview', clientMessageId!, 'retry remains failed', Date.now());
    });
    const rolledBackRetry = await screen.findByRole('button', { name: '重试本轮' });
    expect(rolledBackRetry).toBeEnabled();
    expect(screen.queryByRole('button', { name: '已提交重试' })).not.toBeInTheDocument();
  });



  it('continues from an accepted network-interrupted turn without replaying the failed prompt', async () => {
    const transport = productionTransport({
      'agent.session.snapshot': {
        lastSequence: 7, resumeToken: 'network-failed:7', status: 'idle',
        items: [{
          ...historyMessage('session-preview', 'network-user', 'user', '先执行这轮工作'),
          turnId: 'turn-network-failed', clientMessageId: 'client-network-original',
        }, {
          ...historyMessage('session-preview', 'network-assistant', 'assistant', ''),
          turnId: 'turn-network-failed', status: 'failed',
          blocks: [{ id: 'network-error', type: 'error', status: 'failed',
            presentationKind: 'error', data: { message: 'WebSocket error' } }],
        }],
      },
      'agent.session.prompt': () => new Promise(() => {}),
    });
    const user = userEvent.setup();
    renderAgent(transport);
    await user.click(await screen.findByRole('button', { name: '继续' }));
    await waitFor(() => expect(
      transport.requests.filter((call) => call.pathId === 'agent.session.prompt'),
    ).toHaveLength(1));
    const continuation = transport.requests.find((call) => call.pathId === 'agent.session.prompt')?.body as Record<string, unknown>;
    expect(continuation.message).toBe(
      '继续完成上一轮。请基于当前 Session 已保留的工具结果和文件生成最终回复，不要重复已经完成的操作。',
    );
    expect(continuation.attachments).toEqual([]);
    expect(continuation).not.toHaveProperty('retryOfClientMessageId');
    expect(continuation.clientMessageId).not.toBe('client-network-original');
    expect(screen.queryByRole('button', { name: '重试本轮' })).not.toBeInTheDocument();
  });

  it('waits for an explicit retry before replaying an ambiguous admission', async () => {
    const transport = featureTransport(
      previewModelCatalog('session-preview'),
      { ok: true, items: toolCatalog() },
      { ok: true, items: previewSessions },
      () => {
        throw new TypeError('Failed to fetch');
      },
    );
    const user = userEvent.setup();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await user.type(composer, '网络回执丢失也只能执行一次');
    await user.click(screen.getByRole('button', { name: '发送' }));

    const warning = await screen.findByText(
      /系统不会自动重试/,
    );
    expect(warning).toHaveTextContent(
      '手动重试会核对同一条消息',
    );
    const retry = await screen.findByRole('button', {
      name: '重试本轮',
    });
    const firstAttempt = transport.requests.filter(
      (call) => call.request.pathId === 'agent.session.prompt',
    );
    expect(firstAttempt).toHaveLength(1);

    await user.click(retry);
    await waitFor(() => expect(
      transport.requests.filter(
        (call) => call.request.pathId === 'agent.session.prompt',
      ),
    ).toHaveLength(2));
    const prompts = transport.requests.filter(
      (call) => call.request.pathId === 'agent.session.prompt',
    );
    expect(JSON.stringify(prompts[1]?.request.body)).toBe(
      JSON.stringify(prompts[0]?.request.body),
    );
    const projection = useAgentLiveStore.getState()
      .projections['session-preview'];
    const matchingMessages = Object.values(
      projection.messagesById,
    ).filter((message) => (
      message.role === 'user'
      && message.blocks.some(
        (block) => (
          block.data.text
          === '网络回执丢失也只能执行一次'
        ),
      )
    ));
    expect(matchingMessages).toHaveLength(1);
    expect(matchingMessages[0]).toMatchObject({
      status: 'failed',
      clientMessageId: (
        prompts[0]?.request.body as Record<string, unknown>
      ).clientMessageId,
      admissionState: 'ambiguous',
    });
    expect(
      projection.turnsById[
        matchingMessages[0]?.turnId ?? ''
      ]?.failure,
    ).toContain('系统不会自动重试');
  });

  it('replays an ambiguous native follow-up with the exact original delivery payload', async () => {
    const transport = featureTransport(
      previewModelCatalog('session-preview'),
      { ok: true, items: toolCatalog() },
      { ok: true, items: previewSessions },
      () => {
        throw new TypeError('Failed to fetch');
      },
    );
    const user = userEvent.setup();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(
      useAgentLiveStore.getState().projections['session-preview']?.messageOrder.length,
    ).toBeGreaterThan(0));
    act(() => useAgentLiveStore.getState().appendOptimistic('session-preview', {
      clientMessageId: 'native-ambiguous-active',
      text: '正在执行原任务',
      nowMs: Date.now(),
    }));

    await user.type(composer, '完成后继续整理');
    await user.keyboard('{Enter}');
    const retry = await screen.findByRole('button', { name: '重试本轮' });
    const firstAttempt = transport.requests.filter(
      (call) => call.request.pathId === 'agent.session.prompt',
    );
    expect(firstAttempt).toHaveLength(1);
    expect(firstAttempt[0]?.request.body).toMatchObject({
      message: '完成后继续整理',
      delivery: 'followUp',
    });

    await user.click(retry);
    await waitFor(() => expect(
      transport.requests.filter(
        (call) => call.request.pathId === 'agent.session.prompt',
      ),
    ).toHaveLength(2));
    const prompts = transport.requests.filter(
      (call) => call.request.pathId === 'agent.session.prompt',
    );
    expect(JSON.stringify(prompts[1]?.request.body)).toBe(
      JSON.stringify(prompts[0]?.request.body),
    );
  });

  it('keeps an unresolved pending receipt in confirmation without resending or retry actions', async () => {
    const transport = featureTransport(
      previewModelCatalog('session-preview'),
      { ok: true, items: toolCatalog() },
      { ok: true, items: previewSessions },
      (request: ControlRequest) => {
        const body = request.body as Record<string, unknown>;
        throw new ControlTransportHttpError(
          'agent.session.prompt',
          409,
          'receipt cannot be reconciled',
          {
            ok: false,
            code: 'AGENT_COMMAND_PENDING',
            error: 'receipt cannot be reconciled',
            commandReceipt: {
              state: 'pending',
              clientMessageId: body.clientMessageId,
              recoveryState: 'unresolved',
            },
          },
        );
      },
    );
    const user = userEvent.setup();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', {
      name: '消息',
    });
    await user.type(composer, '这条操作不能重复执行');
    await user.click(screen.getByRole('button', { name: '发送' }));

    const detail = await screen.findByText(
      /无法确认这条消息是否已执行/,
    );
    const confirmation = detail.closest('[role="status"]');
    expect(confirmation).not.toBeNull();
    expect(confirmation).toHaveTextContent('正在确认接收状态');
    expect(confirmation).toHaveTextContent('为避免重复执行');
    expect(detail.closest('[role="alert"]')).toBeNull();
    expect(within(confirmation as HTMLElement).queryByRole(
      'button',
      { name: '重试本轮' },
    )).not.toBeInTheDocument();
    expect(within(confirmation as HTMLElement).queryByRole(
      'button',
      { name: '切换模型' },
    )).not.toBeInTheDocument();

    const prompts = transport.requests.filter(
      (call) => call.request.pathId === 'agent.session.prompt',
    );
    expect(prompts).toHaveLength(1);
    const clientMessageId = String(
      (prompts[0]?.request.body as Record<string, unknown>)
        .clientMessageId,
    );
    const projection = useAgentLiveStore.getState()
      .projections['session-preview'];
    const matchingMessages = Object.values(
      projection.messagesById,
    ).filter((message) => (
      message.clientMessageId === clientMessageId
    ));
    expect(matchingMessages).toHaveLength(1);
    expect(matchingMessages[0]).toMatchObject({
      status: 'queued',
      admissionState: 'unresolved',
    });
  });

  it('projects an in-flight pending receipt without polling or offering retry actions', async () => {
    const transport = featureTransport(
      previewModelCatalog('session-preview'),
      { ok: true, items: toolCatalog() },
      { ok: true, items: previewSessions },
      (request: ControlRequest) => {
        const body = request.body as Record<string, unknown>;
        throw new ControlTransportHttpError(
          'agent.session.prompt',
          409,
          'receipt is still in flight',
          {
            ok: false,
            code: 'AGENT_COMMAND_PENDING',
            error: 'receipt is still in flight',
            commandReceipt: {
              state: 'pending',
              clientMessageId: body.clientMessageId,
              recoveryState: 'in_flight',
            },
          },
        );
      },
    );
    const user = userEvent.setup();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', {
      name: '消息',
    });
    await user.type(composer, '等待服务端确认且不能重复执行');
    await user.click(screen.getByRole('button', { name: '发送' }));

    const detail = await screen.findByText(
      /服务端仍在确认这条消息是否已接收/,
    );
    const confirmation = detail.closest('[role="status"]');
    expect(confirmation).not.toBeNull();
    expect(confirmation).toHaveTextContent('正在确认接收状态');
    expect(confirmation).toHaveTextContent('系统不会自动重试');
    expect(detail.closest('[role="alert"]')).toBeNull();
    expect(within(confirmation as HTMLElement).queryByRole(
      'button',
      { name: '重试本轮' },
    )).not.toBeInTheDocument();
    expect(within(confirmation as HTMLElement).queryByRole(
      'button',
      { name: '切换模型' },
    )).not.toBeInTheDocument();

    const prompts = transport.requests.filter(
      (call) => call.request.pathId === 'agent.session.prompt',
    );
    expect(prompts).toHaveLength(1);
    const clientMessageId = String(
      (prompts[0]?.request.body as Record<string, unknown>)
        .clientMessageId,
    );
    const projection = useAgentLiveStore.getState()
      .projections['session-preview'];
    expect(
      Object.values(projection.messagesById).find(
        (message) => message.clientMessageId === clientMessageId,
      ),
    ).toMatchObject({
      status: 'queued',
      admissionState: 'pending',
    });
  });

  it('does not turn a stale-window active-turn conflict into duplicate chat messages', async () => {
    let attempt = 0;
    const transport = featureTransport(
      previewModelCatalog('session-preview'),
      { ok: true, items: toolCatalog() },
      { ok: true, items: previewSessions },
      (request: ControlRequest) => {
        attempt += 1;
        if (attempt === 1) throw new Error('model unavailable');
        const body = request.body as Record<string, unknown>;
        throw new ControlTransportHttpError(
          'agent.session.prompt',
          409,
          'Pi 正在处理上一轮，请等待结束或停止完成后再发送',
          {
            ok: false,
            code: 'AGENT_COMMAND_FAILED',
            error: 'Pi 正在处理上一轮，请等待结束或停止完成后再发送',
            commandReceipt: {
              state: 'failed',
              clientMessageId: body.clientMessageId,
              causeCode: 'AGENT_TURN_CONFLICT',
            },
          },
        );
      },
    );
    const user = userEvent.setup();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await user.type(composer, '避免重复的同一条消息');
    await user.click(screen.getByRole('button', { name: '发送' }));
    const retry = await screen.findByRole('button', { name: '重试本轮' });
    await user.click(retry);

    await waitFor(() => expect(
      document.querySelector('.agent-conversation__header [role="alert"]')
    ).toHaveTextContent('上一轮仍在处理，输入已保留'));
    await waitFor(() => expect(
      screen.getByRole('button', { name: '重试本轮' }),
    ).toBeEnabled());
    const projection = useAgentLiveStore.getState().projections['session-preview'];
    const matchingUserMessages = Object.values(projection.messagesById).filter((message) => (
      message.role === 'user'
      && message.blocks.some((block) => block.data.text === '避免重复的同一条消息')
    ));
    const matchingFailedTurns = Object.values(projection.turnsById).filter((turn) => (
      turn.status === 'failed'
    ));
    expect(matchingUserMessages).toHaveLength(1);
    expect(matchingFailedTurns).toHaveLength(1);
    expect(transport.requests.filter((call) => call.request.pathId === 'agent.session.prompt')).toHaveLength(2);
  });

  it('opens the real model picker from a failed turn', async () => {
    const transport = featureTransport(
      previewModelCatalog('session-preview'),
      { ok: true, items: toolCatalog() },
      { ok: true, items: previewSessions },
      () => { throw new Error('model unavailable'); },
    );
    const user = userEvent.setup();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await user.type(composer, '切换模型后继续');
    await user.click(screen.getByRole('button', { name: '发送' }));

    await user.click(await screen.findByRole('button', { name: '切换模型' }));

    const picker = await screen.findByRole('dialog', { name: '选择模型与推理强度' });
    expect(within(picker).getByRole('option', { name: '选择模型 GPT-5.4' })).toBeInTheDocument();
  });

  it('unlocks send and continuation when projection status is stale working but the latest turn failed', async () => {
    const failedTurnId = 'turn-provider-failed';
    const transport = productionTransport({
      'agent.session.snapshot': {
        lastSequence: 7,
        resumeToken: 'provider-failed:7',
        status: 'working',
        items: [
          {
            ...historyMessage('session-preview', 'provider-user', 'user', '继续处理这个请求'),
            turnId: failedTurnId,
          },
          {
            ...historyMessage('session-preview', 'provider-assistant', 'assistant', ''),
            turnId: failedTurnId,
            status: 'failed',
            blocks: [{
              id: 'provider-assistant:error',
              type: 'error',
              status: 'failed',
              presentationKind: 'error',
              data: { message: '400 Error from provider (Console Go): Upstream request failed' },
            }],
          },
        ],
      },
      'agent.session.prompt': { ok: true },
    });
    const user = userEvent.setup();
    renderAgent(transport);

    await waitFor(() => expect(useAgentLiveStore.getState().projections['session-preview']?.status).toBe('working'));
    expect(useAgentLiveStore.getState().projections['session-preview']?.turnsById[failedTurnId]?.status).toBe('failed');
    expect(await screen.findByRole('button', { name: /^发送/ })).toBeDisabled();
    expect(screen.queryByRole('button', { name: '停止本轮' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '继续' })).toBeEnabled();
    expect(screen.getByText('模型服务请求失败，请重试或切换模型。')).toBeInTheDocument();
    await user.type(screen.getByRole('textbox', { name: '消息' }), '新的输入');
    expect(screen.getByRole('button', { name: /^发送/ })).toBeEnabled();
  });

  it('submits approval decisions through the real approval route', async () => {
    const transport = featureTransport();
    const user = userEvent.setup();
    renderAgent(transport);
    await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(useAgentLiveStore.getState().projections['session-preview']?.lastSequence).toBeGreaterThan(0));
    const projection = useAgentLiveStore.getState().projections['session-preview'];
    const turnId = projection.turnOrder.at(-1) ?? 'turn-approval';
    act(() => {
      useAgentLiveStore.getState().applyEvents('session-preview', [{
        schemaVersion: 'rag-ime.agent-event.v1',
        eventId: 'approval-pending-ui',
        sessionId: 'session-preview',
        turnId,
        sequence: projection.lastSequence + 1,
        createdAtMs: Date.now(),
        streamKind: 'agent',
        eventType: 'approval_required',
        payload: {
          approvalId: 'approval-live-1',
          payloadSha256: 'approval-live-hash',
          summary: '应用这次输入法配置变更',
          riskLevel: 'R2',
          preview: {
            title: '确认输入法配置变更',
            summary: '关闭模糊音并重新部署输入方案',
            changes: [
              { label: '模糊音', before: '开启', after: '关闭' },
              { label: '输入方案', before: '', after: '小鹤双拼' },
            ],
          },
        },
        resumeToken: 'approval-pending-ui',
      }]);
    });

    expect(await screen.findByRole('heading', { name: '确认输入法配置变更' })).toBeInTheDocument();
    expect(screen.getByText('模糊音')).toBeInTheDocument();
    expect(screen.getByText('开启')).toBeInTheDocument();
    expect(screen.getByText('关闭')).toBeInTheDocument();
    await user.click(await screen.findByRole('button', { name: '批准并执行' }));

    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'agent.approval.decide',
        params: { approvalId: 'approval-live-1' },
        body: { decision: 'approve', payloadSha256: 'approval-live-hash' },
      }),
    })));
  });

  it('opens the existing approval review from a failed tool receipt and uses its bound route', async () => {
    const transport = featureTransport();
    const user = userEvent.setup();
    const view = renderAgent(transport);
    await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(useAgentLiveStore.getState().projections['session-preview']?.lastSequence).toBeGreaterThan(0));
    const projection = useAgentLiveStore.getState().projections['session-preview'];
    const turnId = projection.turnOrder.at(-1) ?? 'turn-tool-approval';
    act(() => {
      useAgentLiveStore.getState().applyEvents('session-preview', [{
        schemaVersion: 'rag-ime.agent-event.v1',
        eventId: 'tool-approval-failed-ui',
        sessionId: 'session-preview',
        turnId,
        sequence: projection.lastSequence + 1,
        createdAtMs: Date.now(),
        streamKind: 'agent',
        eventType: 'tool_finished',
        payload: {
          toolCallId: 'tool-approval-failed-ui',
          toolName: 'input',
          isError: true,
          approvalId: 'approval-from-tool-1',
          payloadSha256: 'd'.repeat(64),
          preview: {
            title: '确认失败工具的受控操作',
            summary: '应用输入法设置',
            changes: [{ label: '候选数', before: '5', after: '7' }],
          },
          result: {
            details: {
              ok: false,
              operation: 'apply_settings',
              result: { error: '该操作需要本机审批后继续。' },
            },
          },
        },
        resumeToken: 'tool-approval-failed-ui',
      }]);
    });

    const activity = [...view.container.querySelectorAll<HTMLElement>('.agent-activity--inline')]
      .find((item) => item.textContent?.includes('该操作需要本机审批后继续'));
    expect(activity).toBeDefined();
    expect(activity).not.toHaveAttribute('open');
    await user.click(activity!.querySelector('summary')!);
    expect(activity).toHaveAttribute('open');
    expect(screen.queryByRole('dialog', { name: '操作记录' })).not.toBeInTheDocument();
    const failedRow = [...activity!.querySelectorAll<HTMLDetailsElement>('.agent-activity-row')]
      .find((row) => row.textContent?.includes('该操作需要本机审批后继续'));
    expect(failedRow).toBeDefined();
    expect(failedRow).not.toHaveAttribute('open');
    await user.click(failedRow!.querySelector('summary')!);
    expect(failedRow).toHaveAttribute('open');
    await user.click(within(failedRow!).getByRole('button', { name: '去审批' }));

    const dialog = await screen.findByRole('dialog', { name: '确认失败工具的受控操作' });
    expect(within(dialog).getByText('候选数')).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: '批准并执行' }));
    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'agent.approval.decide',
        params: { approvalId: 'approval-from-tool-1' },
        body: { decision: 'approve', payloadSha256: 'd'.repeat(64) },
      }),
    })));
  });

  it('opens the real permission picker from a permission-denied tool receipt', async () => {
    const transport = featureTransport();
    const user = userEvent.setup();
    const view = renderAgent(transport);
    await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(useAgentLiveStore.getState().projections['session-preview']?.lastSequence).toBeGreaterThan(0));
    const projection = useAgentLiveStore.getState().projections['session-preview'];
    const turnId = projection.turnOrder.at(-1) ?? 'turn-tool-permission';
    act(() => {
      useAgentLiveStore.getState().applyEvents('session-preview', [{
        schemaVersion: 'rag-ime.agent-event.v1',
        eventId: 'tool-permission-failed-ui',
        sessionId: 'session-preview',
        turnId,
        sequence: projection.lastSequence + 1,
        createdAtMs: Date.now(),
        streamKind: 'agent',
        eventType: 'tool_finished',
        payload: {
          toolCallId: 'tool-permission-failed-ui',
          toolName: 'workspace_shell',
          isError: true,
          result: { details: { ok: false, result: { error: '工作区不在授权目录内，当前权限不足。' } } },
        },
        resumeToken: 'tool-permission-failed-ui',
      }]);
    });

    const activity = [...view.container.querySelectorAll<HTMLElement>('.agent-activity--inline')]
      .find((item) => item.textContent?.includes('工作区不在授权目录内'));
    expect(activity).toBeDefined();
    expect(activity).not.toHaveAttribute('open');
    await user.click(activity!.querySelector('summary')!);
    expect(activity).toHaveAttribute('open');
    expect(screen.queryByRole('dialog', { name: '操作记录' })).not.toBeInTheDocument();
    const failedRow = [...activity!.querySelectorAll<HTMLDetailsElement>('.agent-activity-row')]
      .find((row) => row.textContent?.includes('工作区不在授权目录内'));
    expect(failedRow).toBeDefined();
    expect(failedRow).not.toHaveAttribute('open');
    await user.click(failedRow!.querySelector('summary')!);
    expect(failedRow).toHaveAttribute('open');
    await user.click(within(failedRow!).getByRole('button', { name: '请求权限' }));

    expect(await screen.findByText('对话权限')).toBeInTheDocument();
    const picker = document.querySelector('.agent-picker-popover');
    expect(picker).not.toBeNull();
    expect(within(picker as HTMLElement).getByRole('radio', { name: /完全访问/ })).toBeInTheDocument();
  });

  it('restores a pending approval dialog directly from the session snapshot', async () => {
    const snapshot = previewAgentSnapshot('session-preview');
    const transport = productionTransport({
      'agent.session.snapshot': {
        ...snapshot,
        status: 'busy',
        liveEvents: [{
          schemaVersion: 'rag-ime.agent-event.v1',
          eventId: 'session-preview:snapshot:approval-snapshot-1',
          sessionId: 'session-preview',
          turnId: 'approval:approval-snapshot-1',
          sequence: 12,
          createdAtMs: Date.now(),
          eventType: 'approval_required',
          payload: {
            approvalId: 'approval-snapshot-1',
            payloadSha256: 'c'.repeat(64),
            summary: '恢复后继续审批',
            preview: { title: '恢复待审批操作', changes: [] },
          },
          resumeToken: 'session-preview:snapshot:approval-snapshot-1',
        }],
      },
    });
    renderAgent(transport);

    expect(await screen.findByRole('dialog', { name: '恢复待审批操作' })).toBeInTheDocument();
    expect(useAgentLiveStore.getState().projections['session-preview']?.status).toBe('waiting');
  });

  it('keeps approval failures visible inside the forced review dialog', async () => {
    const transport = featureTransport(
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      async () => { throw new Error('审批已过期，请重新发起操作'); },
    );
    const user = userEvent.setup();
    renderAgent(transport);
    await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(useAgentLiveStore.getState().projections['session-preview']?.lastSequence).toBeGreaterThan(0));
    const projection = useAgentLiveStore.getState().projections['session-preview'];
    const turnId = projection.turnOrder.at(-1) ?? 'turn-approval-error';
    act(() => {
      useAgentLiveStore.getState().applyEvents('session-preview', [{
        schemaVersion: 'rag-ime.agent-event.v1',
        eventId: 'approval-pending-error-ui',
        sessionId: 'session-preview',
        turnId,
        sequence: projection.lastSequence + 1,
        createdAtMs: Date.now(),
        streamKind: 'agent',
        eventType: 'approval_required',
        payload: {
          approvalId: 'approval-live-error',
          payloadSha256: 'approval-live-error-hash',
          summary: '应用设置',
          preview: { title: '确认应用设置', changes: [{ label: '候选数', before: '5', after: '7' }] },
        },
        resumeToken: 'approval-pending-error-ui',
      }]);
    });

    await user.click(await screen.findByRole('button', { name: '批准并执行' }));

    const dialog = await screen.findByRole('dialog', { name: '确认应用设置' });
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('审批已过期，请重新发起操作');
    expect(within(dialog).getByRole('button', { name: '批准并执行' })).toBeEnabled();
  });

  it('keeps a late approval failure inside the Session that owned the decision', async () => {
    const pendingApproval = deferred<unknown>();
    const transport = featureTransport(
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      () => pendingApproval.promise,
    );
    const user = userEvent.setup();
    renderAgent(transport);
    await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(
      useAgentLiveStore.getState().projections['session-preview']?.lastSequence,
    ).toBeGreaterThan(0));
    const projection = useAgentLiveStore.getState().projections['session-preview'];
    const turnId = projection.turnOrder.at(-1) ?? 'turn-approval-switch';
    act(() => {
      useAgentLiveStore.getState().applyEvents('session-preview', [{
        schemaVersion: 'rag-ime.agent-event.v1',
        eventId: 'approval-before-session-switch',
        sessionId: 'session-preview',
        turnId,
        sequence: projection.lastSequence + 1,
        createdAtMs: Date.now(),
        streamKind: 'agent',
        eventType: 'approval_required',
        payload: {
          approvalId: 'approval-session-a',
          payloadSha256: 'approval-session-a-hash',
          summary: '执行 A 的受控操作',
          preview: { title: '确认 A 的受控操作', changes: [] },
        },
        resumeToken: 'approval-before-session-switch',
      }]);
    });

    await user.click(await screen.findByRole('button', { name: '批准并执行' }));
    await waitFor(() => expect(
      transport.requests.filter((call) => call.request.pathId === 'agent.approval.decide'),
    ).toHaveLength(1));

    // A modal blocks pointer navigation, but the selected Session can still
    // change through restored navigation/native state while the request is in
    // flight. Exercise that ownership boundary directly.
    fireEvent.click(screen.getByRole('button', { name: '记忆整理', hidden: true }));
    const composerB = await screen.findByRole('textbox', { name: '消息' });
    await user.type(composerB, 'B 的草稿不能被 A 的审批结果覆盖');
    await act(async () => pendingApproval.reject(new Error('A 的审批已经过期')));

    expect(composerB).toHaveValue('B 的草稿不能被 A 的审批结果覆盖');
    expect(screen.queryByText('A 的审批已经过期')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '控制中心迁移' }));
    expect(await screen.findByText('A 的审批已经过期')).toBeInTheDocument();
  });

  it('keeps the turn busy while abort is only acknowledged and prevents repeated stop clicks', async () => {
    let resolveAbort!: (value: unknown) => void;
    const abortPending = new Promise((resolve) => { resolveAbort = resolve; });
    const transport = featureTransport(
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      () => abortPending,
    );
    renderAgent(transport);
    await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(useAgentLiveStore.getState().projections['session-preview']?.lastSequence).toBeGreaterThan(0));
    act(() => {
      useAgentLiveStore.getState().appendOptimistic('session-preview', {
        clientMessageId: 'stop-in-flight',
        text: '停止这轮',
        nowMs: Date.now(),
      });
    });

    fireEvent.click(await screen.findByRole('button', { name: '停止本轮' }));

    const stopping = screen.getByRole('button', { name: '正在停止本轮' });
    expect(stopping).toBeDisabled();
    expect(stopping).toHaveAttribute('aria-busy', 'true');
    expect(useAgentLiveStore.getState().projections['session-preview']?.status).toBe('busy');
    const abortRequests = transport.requests.filter(({ request }) => request.pathId === 'agent.session.abort');
    expect(abortRequests).toHaveLength(1);
    expect(abortRequests[0]?.request.body).toEqual({});
    resolveAbort({ ok: true });
  });

  it('keeps acknowledged Stop disabled until terminal SSE without requesting full history', async () => {
    const complete = previewAgentSnapshot('session-preview');
    const transport = productionTransport({
      'agent.session.abort': { ok: true },
      'agent.session.snapshot': (request: ControlRequest) => {
        if (request.query?.view !== 'recent') {
          throw new Error('Stop must wait for terminal SSE before archive reconciliation');
        }
        return {
          ...busyStopSnapshot('session-preview', complete.lastSequence),
          snapshotScope: 'recent',
          partial: true,
          recentFromSequence: complete.lastSequence,
        };
      },
    });

    renderAgent(transport);
    await waitFor(() => expect(
      transport.subscriptionCount('agent.session.events'),
    ).toBe(1));
    fireEvent.click(await screen.findByRole('button', { name: '停止本轮' }));

    expect(await screen.findByRole('button', { name: '正在停止本轮' })).toBeDisabled();
    expect(transport.requests.filter((request) => (
      request.pathId === 'agent.session.snapshot'
      && request.query?.view === undefined
    ))).toHaveLength(0);

    const projection = useAgentLiveStore.getState().projections['session-preview'];
    const sequence = projection.lastSequence + 1;
    act(() => {
      transport.emit('agent.session.events', {
        schemaVersion: 'rag-ime.agent-event.v1',
        eventId: 'acknowledged-stop-terminal',
        sessionId: 'session-preview',
        turnId: 'session-preview:turn-stop-reconcile',
        sequence,
        createdAtMs: Date.now(),
        streamKind: 'agent',
        eventType: 'turn_failed',
        payload: { error: 'aborted', status: 'aborted' },
        resumeToken: `session-preview:${sequence}`,
      });
    });
    await waitFor(() => expect(
      screen.queryByRole('button', { name: '正在停止本轮' }),
    ).not.toBeInTheDocument());
  });

  it('settles pending admission from the abort receipt without requesting full history', async () => {
    const complete = previewAgentSnapshot('session-preview');
    const transport = productionTransport({
      'agent.session.abort': {
        schemaVersion: 'rag-ime.agent-abort.v1',
        ok: true,
        sessionId: 'session-preview',
        runtimeReceipt: {
          schemaVersion: 'rag-ime.pi-session-abort-receipt.v1',
          sessionId: 'session-preview',
          turnId: '',
          pendingAdmission: true,
          admissionCancelled: true,
          lifecycle: {
            schemaVersion: 'pi.agent-abort-receipt.v1',
            scopeId: 'session-preview',
            generation: 0,
            reason: 'user_abort',
            pendingOperations: [],
            drained: true,
            idle: true,
          },
        },
        approvalCancellation: {},
      },
      'agent.session.snapshot': (request: ControlRequest) => {
        if (request.query?.view !== 'recent') {
          throw new Error('pending admission must not wait for full history');
        }
        return {
          ...complete,
          status: 'busy',
          snapshotScope: 'recent',
          partial: true,
          recentFromSequence: complete.lastSequence,
        };
      },
    });

    renderAgent(transport);
    await waitFor(() => expect(
      transport.subscriptionCount('agent.session.events'),
    ).toBe(1));
    act(() => {
      useAgentLiveStore.getState().appendOptimistic('session-preview', {
        clientMessageId: 'pending-admission-stop',
        text: '在 admission 阶段停止',
        nowMs: Date.now(),
      });
    });
    fireEvent.click(await screen.findByRole('button', { name: '停止本轮' }));

    await waitFor(() => expect(
      useAgentLiveStore.getState().projections['session-preview']?.optimisticByClientMessageId,
    ).toEqual({}));
    expect(transport.requests.filter((request) => (
      request.pathId === 'agent.session.snapshot'
      && request.query?.view === undefined
    ))).toHaveLength(0);
  });

  it('settles a pre-dispatch stop from the explicit cancellation receipt without leaving a ghost turn', async () => {
    const pendingPrompt = deferred<unknown>();
    let snapshotCalls = 0;
    const transport = featureTransport(
      undefined,
      undefined,
      undefined,
      () => pendingPrompt.promise,
      undefined,
      undefined,
      {
        schemaVersion: 'rag-ime.agent-abort.v1',
        ok: true,
        sessionId: 'session-preview',
        runtimeReceipt: {
          schemaVersion: 'rag-ime.pi-session-abort-receipt.v1',
          sessionId: 'session-preview',
          turnId: '',
          pendingAdmission: true,
          admissionCancelled: true,
          lifecycle: {
            schemaVersion: 'pi.agent-abort-receipt.v1',
            scopeId: 'session-preview',
            generation: 0,
            reason: 'user_abort',
            pendingOperations: [],
            drained: true,
            idle: true,
          },
        },
        approvalCancellation: {},
      },
      undefined,
      () => {
        snapshotCalls += 1;
        if (snapshotCalls === 1) return previewAgentSnapshot('session-preview');
        return {
          schemaVersion: 'rag-ime.agent-message-list.v1',
          ok: true,
          sessionId: 'session-preview',
          items: [],
          liveEvents: [],
          status: 'idle',
          lastSequence: 100,
          resumeToken: 'session-preview:100',
        };
      },
    );
    const user = userEvent.setup();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await user.type(composer, '发送前就停止这一轮');
    await user.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(
      transport.requests.filter((call) => call.request.pathId === 'agent.session.prompt'),
    ).toHaveLength(1));
    fireEvent.click(await screen.findByRole('button', { name: '停止本轮' }));

    expect(snapshotCalls).toBe(1);
    await waitFor(() => expect(
      screen.queryByRole('button', { name: '正在停止本轮' }),
    ).not.toBeInTheDocument());
    await waitFor(() => expect(
      useAgentLiveStore.getState().projections['session-preview']?.status,
    ).toBe('idle'));
    expect(useAgentLiveStore.getState().projections['session-preview']?.optimisticByClientMessageId).toEqual({});
    expect(Object.values(
      useAgentLiveStore.getState().projections['session-preview']?.messagesById ?? {},
    ).some((message) => message.blocks.some(
      (block) => block.data.text === '发送前就停止这一轮',
    ))).toBe(false);

    pendingPrompt.resolve({
      accepted: false,
      cancelled: true,
      abortRequested: true,
      admissionCancelled: true,
    });
    await user.type(composer, '下一轮仍可发送');
    await waitFor(() => expect(screen.getByRole('button', { name: '发送' })).toBeEnabled());
    expect(useAgentLiveStore.getState().projections['session-preview']?.optimisticByClientMessageId).toEqual({});
  });

  it('removes an optimistic prompt when Pi reports that admission was cancelled', async () => {
    const transport = featureTransport(
      undefined,
      undefined,
      undefined,
      {
        accepted: false,
        cancelled: true,
        abortRequested: true,
        admissionCancelled: true,
      },
    );
    const user = userEvent.setup();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await user.type(composer, '这条消息不应成为幽灵回合');
    await user.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(
      transport.requests.filter((call) => call.request.pathId === 'agent.session.prompt'),
    ).toHaveLength(1));
    await waitFor(() => expect(
      useAgentLiveStore.getState().projections['session-preview']?.optimisticByClientMessageId,
    ).toEqual({}));
    expect(Object.values(
      useAgentLiveStore.getState().projections['session-preview']?.messagesById ?? {},
    ).some((message) => message.blocks.some(
      (block) => block.data.text === '这条消息不应成为幽灵回合',
    ))).toBe(false);
    expect(screen.queryByText('思考中')).not.toBeInTheDocument();
  });

  it('keeps a stale client-side busy turn until terminal SSE starts quiet reconciliation', async () => {
    let snapshotCalls = 0;
    const transport = productionTransport({
      'agent.session.abort': { ok: true },
      'agent.session.snapshot': () => {
        snapshotCalls += 1;
        if (snapshotCalls <= 2) {
          return busyStopSnapshot('session-preview', 98 + snapshotCalls);
        }
        return {
          schemaVersion: 'rag-ime.agent-message-list.v1',
          ok: true,
          sessionId: 'session-preview',
          items: [],
          liveEvents: [],
          status: 'idle',
          lastSequence: 101,
          resumeToken: 'session-preview:101',
        };
      },
    });
    const user = userEvent.setup();
    renderAgent(transport);
    await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(snapshotCalls).toBe(1));
    await waitFor(() => expect(
      useAgentLiveStore.getState().projections['session-preview']?.status,
    ).toBe('busy'));
    expect(useAgentLiveStore.getState().projections['session-preview']?.turnsById['session-preview:turn-stop-reconcile']?.status).toBe('running');
    await user.click(await screen.findByRole('button', { name: '停止本轮' }));

    expect(await screen.findByRole('button', { name: '正在停止本轮' })).toBeDisabled();
    expect(snapshotCalls).toBe(1);
    expect(useAgentLiveStore.getState().projections['session-preview']?.status).toBe('busy');
    const projection = useAgentLiveStore.getState().projections['session-preview'];
    const sequence = projection.lastSequence + 1;
    act(() => {
      transport.emit('agent.session.events', {
        schemaVersion: 'rag-ime.agent-event.v1',
        eventId: 'stop-terminal-before-stale-snapshot',
        sessionId: 'session-preview',
        turnId: 'session-preview:turn-stop-reconcile',
        sequence,
        createdAtMs: Date.now(),
        streamKind: 'agent',
        eventType: 'turn_failed',
        payload: { error: 'aborted', status: 'aborted' },
        resumeToken: `session-preview:${sequence}`,
      });
    });

    await waitFor(() => expect(snapshotCalls).toBe(2));
    await waitFor(() => expect(useAgentLiveStore.getState().projections['session-preview']?.status).toBe('failed'));
    await waitFor(() => expect(
      screen.queryByRole('button', { name: '正在停止本轮' }),
    ).not.toBeInTheDocument());
  });

  it('returns after abort ACK, then converges from a late terminal snapshot', async () => {
    let snapshotCalls = 0;
    let terminalPersisted = false;
    const transport = productionTransport({
      'agent.session.abort': { ok: true },
      'agent.session.snapshot': () => {
        snapshotCalls += 1;
        return terminalPersisted
          ? abortedStopSnapshot('session-preview', 200 + snapshotCalls)
          : busyStopSnapshot('session-preview', 100 + snapshotCalls);
      },
    });
    renderAgent(transport);
    await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(snapshotCalls).toBe(1));
    await waitFor(() => expect(
      useAgentLiveStore.getState().projections['session-preview']?.status,
    ).toBe('busy'));
    fireEvent.click(await screen.findByRole('button', { name: '停止本轮' }));

    expect(screen.getByRole('button', { name: '正在停止本轮' })).toBeDisabled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(snapshotCalls).toBe(1);

    terminalPersisted = true;
    const projection = useAgentLiveStore.getState().projections['session-preview'];
    const sequence = projection.lastSequence + 1;
    act(() => {
      transport.emit('agent.session.events', {
        schemaVersion: 'rag-ime.agent-event.v1',
        eventId: 'late-stop-terminal',
        sessionId: 'session-preview',
        turnId: 'session-preview:turn-stop-reconcile',
        sequence,
        createdAtMs: Date.now(),
        streamKind: 'agent',
        eventType: 'turn_failed',
        payload: { error: 'aborted', status: 'aborted' },
        resumeToken: `session-preview:${sequence}`,
      });
    });

    await waitFor(() => expect(
      useAgentLiveStore.getState().projections['session-preview']?.status,
    ).toBe('idle'));
    await waitFor(() => expect(snapshotCalls).toBe(2));
    expect(screen.getAllByText('停止当前回合')).toHaveLength(1);
    expect(screen.queryByText('partial')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '停止本轮' })).not.toBeInTheDocument();
  });

  it('treats an open Pi transcript as quiescent instead of showing a permanent stop action', async () => {
    const snapshot = {
      ...previewAgentSnapshot('session-preview'),
      status: 'active',
    };
    const transport = featureTransport(
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      snapshot,
    );

    renderAgent(transport);

    await waitFor(() => expect(useAgentLiveStore.getState().projections['session-preview']?.turnsById['session-preview:turn-media']?.status).toBe('completed'));
    expect(document.querySelector('.agent-assistant-message')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '停止本轮' })).not.toBeInTheDocument();
    expect(screen.queryByText('思考中')).not.toBeInTheDocument();
  });

  it('keeps the composer mounted beside an in-place question card while the turn waits for input', async () => {
    const transport = featureTransport();
    renderAgent(transport);
    await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(
      useAgentLiveStore.getState().projections['session-preview']?.lastSequence,
    ).toBeGreaterThan(0));
    const projection = useAgentLiveStore.getState().projections['session-preview'];
    const turnId = projection.turnOrder.at(-1) ?? 'turn-user-input';

    act(() => {
      useAgentLiveStore.getState().applyEvents('session-preview', [{
        schemaVersion: 'rag-ime.agent-event.v1',
        eventId: 'grouped-user-input-inline',
        sessionId: 'session-preview',
        turnId,
        sequence: projection.lastSequence + 1,
        createdAtMs: Date.now(),
        streamKind: 'agent',
        eventType: 'user_input_required',
        payload: {
          requestId: 'grouped-user-input-inline',
          requestKind: 'grouped_questions',
          title: '确认交付方式',
          questions: [{
            id: 'delivery',
            question: '这次怎么交付？',
            options: [{ label: '直接提交' }, { label: '先看预览' }],
          }],
        },
        resumeToken: 'grouped-user-input-inline',
      }]);
    });

    const card = await screen.findByRole('region', { name: '确认交付方式' });
    expect(card.parentElement).toHaveClass('agent-composer-dock');
    expect(screen.getByRole('textbox', { name: '消息' })).toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: '确认交付方式' })).not.toBeInTheDocument();
  });

  it('opens memory review immediately and resumes Pi when the user defers it', async () => {
    const transport = featureTransport();
    const user = userEvent.setup();
    renderAgent(transport);
    await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(useAgentLiveStore.getState().projections['session-preview']?.lastSequence).toBeGreaterThan(0));
    const projection = useAgentLiveStore.getState().projections['session-preview'];
    const turnId = projection.turnOrder.at(-1) ?? 'turn-review';
    act(() => {
      useAgentLiveStore.getState().applyEvents('session-preview', [{
        schemaVersion: 'rag-ime.agent-event.v1',
        eventId: 'memory-review-pending-ui',
        sessionId: 'session-preview',
        turnId,
        sequence: projection.lastSequence + 1,
        createdAtMs: Date.now(),
        streamKind: 'agent',
        eventType: 'user_input_required',
        payload: {
          requestId: 'ui-review-1',
          requestKind: 'memory_review',
          runId: 'memory-run-1',
          title: '审阅记忆草案',
        },
        resumeToken: 'memory-review-pending-ui',
      }]);
    });

    expect(await screen.findByRole('dialog')).toHaveTextContent('记忆整理草案');
    expect(await screen.findByText('输入法记忆增量整理')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '稍后审阅' }));

    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'agent.session.review.resolve',
        params: { sessionId: 'session-preview' },
        body: { runId: 'memory-run-1', decision: 'deferred' },
      }),
    })));
  });

  it('closes an already-applied memory review by resuming the paused Pi turn', async () => {
    const appliedRun = memoryRunFixture();
    appliedRun.run.status = 'applied';
    appliedRun.run.pendingDiffCount = 0;
    appliedRun.run.changes = appliedRun.run.changes.map((change) => ({
      ...change,
      status: 'applied',
    }));
    const transport = productionTransport({
      'agent.memoryMaintenance.run': appliedRun,
      'agent.session.review.resolve': { ok: true },
    });
    renderAgent(transport);
    await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(
      useAgentLiveStore.getState().projections['session-preview']?.lastSequence,
    ).toBeGreaterThan(0));
    const projection = useAgentLiveStore.getState().projections['session-preview'];
    const turnId = projection.turnOrder.at(-1) ?? 'turn-applied-memory-review';
    act(() => {
      useAgentLiveStore.getState().applyEvents('session-preview', [{
        schemaVersion: 'rag-ime.agent-event.v1',
        eventId: 'memory-review-already-applied',
        sessionId: 'session-preview',
        turnId,
        sequence: projection.lastSequence + 1,
        createdAtMs: Date.now(),
        streamKind: 'agent',
        eventType: 'user_input_required',
        payload: {
          requestId: 'memory-review-already-applied-request',
          requestKind: 'memory_review',
          runId: 'memory-run-1',
          title: '审阅记忆草案',
        },
        resumeToken: 'memory-review-already-applied',
      }]);
    });

    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      pathId: 'agent.session.review.resolve',
      params: { sessionId: 'session-preview' },
      body: { runId: 'memory-run-1', decision: 'reviewed' },
    })));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(transport.requests.some((call) => (
      call.pathId === 'knowledge.database.apply.preview'
    ))).toBe(false);
  });

  it('keeps a late memory-review failure inside its source Session', async () => {
    const pendingReview = deferred<unknown>();
    const transport = productionTransport({
      'agent.memoryMaintenance.run': memoryRunFixture(),
      'agent.session.review.resolve': () => pendingReview.promise,
    });
    const user = userEvent.setup();
    renderAgent(transport);
    await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(
      useAgentLiveStore.getState().projections['session-preview']?.lastSequence,
    ).toBeGreaterThan(0));
    const projection = useAgentLiveStore.getState().projections['session-preview'];
    const turnId = projection.turnOrder.at(-1) ?? 'turn-review-switch';
    act(() => {
      useAgentLiveStore.getState().applyEvents('session-preview', [{
        schemaVersion: 'rag-ime.agent-event.v1',
        eventId: 'memory-review-before-session-switch',
        sessionId: 'session-preview',
        turnId,
        sequence: projection.lastSequence + 1,
        createdAtMs: Date.now(),
        streamKind: 'agent',
        eventType: 'user_input_required',
        payload: {
          requestId: 'review-session-a',
          requestKind: 'memory_review',
          runId: 'memory-run-1',
          title: '审阅 A 的记忆草案',
        },
        resumeToken: 'memory-review-before-session-switch',
      }]);
    });

    await screen.findByRole('dialog');
    await user.click(screen.getByRole('button', { name: '稍后审阅' }));
    await waitFor(() => expect(
      transport.requests.filter((call) => call.pathId === 'agent.session.review.resolve'),
    ).toHaveLength(1));

    fireEvent.click(screen.getByRole('button', { name: '记忆整理', hidden: true }));
    const composerB = await screen.findByRole('textbox', { name: '消息' });
    await user.type(composerB, 'B 的草稿不能被 A 的记忆审阅覆盖');
    await act(async () => pendingReview.reject(new Error('A 的记忆草案已经失效')));

    expect(composerB).toHaveValue('B 的草稿不能被 A 的记忆审阅覆盖');
    expect(screen.queryByText('A 的记忆草案已经失效')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '控制中心迁移' }));
    expect(await screen.findByText('A 的记忆草案已经失效')).toBeInTheDocument();
  });

  it('uses the Pi RPC command catalog and supports keyboard and pointer selection', async () => {
    const transport = featureTransport();
    const user = userEvent.setup();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.session.commands')).toBe(true));

    await user.type(composer, '/');
    expect(composer).toHaveAttribute('aria-controls', 'agent-command-palette');
    const activeCommandOptionId = composer.getAttribute('aria-activedescendant');
    expect(activeCommandOptionId).toMatch(/^agent-command-option-\d+$/);
    expect(document.getElementById(activeCommandOptionId!)).toHaveAttribute('aria-selected', 'true');
    const newCommand = screen.getByRole('option', { name: /\/new/ });
    expect(newCommand).toBeInTheDocument();
    expect(newCommand.querySelector('.agent-command-palette__icon svg')).toBeInTheDocument();
    expect(newCommand).toHaveAttribute('data-source', 'product');
    expect(screen.getByRole('option', { name: /\/resume/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /\/name/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /\/model/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /\/thinking/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /\/permissions/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /\/tools/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /\/session/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /\/status/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /\/subagents/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /\/settings/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /\/hotkeys/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /\/help/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /\/review.*Pi 扩展/ })).toBeInTheDocument();

    await user.keyboard('{Tab}');
    expect(composer).toHaveValue('/new ');
    expect(screen.queryByRole('dialog', { name: '新建对话' })).not.toBeInTheDocument();
    expect(screen.queryByRole('listbox', { name: '命令面板' })).not.toBeInTheDocument();
    await user.keyboard('{Enter}');
    expect(await screen.findByRole('dialog', { name: '新建对话' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '关闭' }));

    await user.clear(composer);
    await user.type(composer, '/rev');
    expect(screen.getByRole('option', { name: /\/review/ })).toBeInTheDocument();
    expect(screen.queryByText('/memory')).not.toBeInTheDocument();
    await user.keyboard('{Tab}');
    expect(composer).toHaveValue('/review ');
    expect(screen.queryByRole('listbox', { name: '命令面板' })).not.toBeInTheDocument();

    await user.clear(composer);
    await user.type(composer, '/rev');
    expect(screen.getByRole('listbox', { name: '命令面板' })).toBeInTheDocument();
    act(() => (composer as HTMLTextAreaElement).blur());
    await waitFor(() => expect(
      screen.queryByRole('listbox', { name: '命令面板' }),
    ).not.toBeInTheDocument());

    await user.click(composer);
    await user.clear(composer);
    await user.type(composer, '/n');
    await user.keyboard('{ArrowDown}{Enter}');
    expect(composer).toHaveValue('/name ');

    await user.clear(composer);
    await user.type(composer, '/skill');
    await user.click(screen.getByRole('option', { name: /\/skill:browser/ }));
    expect(composer).toHaveValue('/skill:browser ');

    await user.clear(composer);
    await user.type(composer, '/');
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('listbox', { name: '命令面板' })).not.toBeInTheDocument();
    expect(composer).not.toHaveAttribute('aria-controls');
    expect(composer).not.toHaveAttribute('aria-activedescendant');

    await user.clear(composer);
    await user.type(composer, '/not-a-real-command');
    await user.click(screen.getByRole('button', { name: '发送' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('未发送给模型');
    expect(transport.requests.some((call) => call.request.pathId === 'agent.session.prompt')).toBe(false);
  });

  it('routes product commands to existing UI and session APIs without prompting the model', async () => {
    const transport = featureTransport();
    const user = userEvent.setup();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.session.commands')).toBe(true));
    expect(transport.requests
      .filter((call) => call.request.pathId === 'agent.tools.list')
      .every((call) => call.request.query?.sessionId === 'session-preview')).toBe(true);

    const openCommandPalette = async () => {
      await user.clear(composer);
      await user.type(composer, '/');
    };

    await openCommandPalette();
    await user.click(screen.getByRole('option', { name: /\/model/ }));
    expect(await screen.findByRole('dialog', { name: '选择模型与推理强度' })).toBeInTheDocument();
    await user.keyboard('{Escape}');

    await openCommandPalette();
    await user.click(screen.getByRole('option', { name: /\/permissions/ }));
    expect(await screen.findByText('对话权限')).toBeInTheDocument();
    const permissionPicker = document.querySelector('.agent-picker-popover');
    expect(permissionPicker).not.toBeNull();
    const availablePermissions = within(permissionPicker as HTMLElement).getAllByRole('radio');
    expect(availablePermissions).toHaveLength(4);
    expect(within(permissionPicker as HTMLElement).getByRole('radio', { name: /^只读/ })).toBeInTheDocument();
    expect(within(permissionPicker as HTMLElement).getByRole('radio', { name: /完全访问/ })).toBeInTheDocument();
    expect(within(permissionPicker as HTMLElement).getByRole('radio', { name: /^工作区托管/ })).toBeInTheDocument();
    expect(within(permissionPicker as HTMLElement).getByRole('radio', { name: /全自动/ })).toBeInTheDocument();
    const fullAccessPermission = within(permissionPicker as HTMLElement).getByRole('radio', { name: /完全访问/ });
    expect(fullAccessPermission).not.toBeDisabled();
    await user.click(fullAccessPermission);
    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'agent.session.mode.update',
        params: { sessionId: 'session-preview' },
        body: {
          mode: 'coordinator',
          executionMode: 'per_action',
          workspaceRoots: ['/Users/example/Projects/personal-agent-workbench', '/'],
          toolProfileVersion: 'control-center-full-access-v1',
          toolAllowlistMode: 'profile',
        },
      }),
    })));
    expect(await screen.findByRole('button', { name: '对话权限：完全访问' })).toBeInTheDocument();

    await openCommandPalette();
    await user.click(screen.getByRole('option', { name: /\/tools/ }));
    const toolPicker = document.querySelector('.agent-tool-picker');
    expect(toolPicker).not.toBeNull();
    expect(within(toolPicker as HTMLElement).getByText('当前对话工具')).toBeInTheDocument();
    await user.keyboard('{Escape}');

    await openCommandPalette();
    await user.click(screen.getByRole('option', { name: /\/name/ }));
    expect(composer).toHaveValue('/name ');
    await user.type(composer, '  新的   对话名称  ');
    await user.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'agent.session.rename',
        params: { sessionId: 'session-preview' },
        body: { title: '新的 对话名称' },
      }),
    })));
    expect((await screen.findAllByText('新的 对话名称')).length).toBeGreaterThan(0);

    await openCommandPalette();
    await user.click(screen.getByRole('option', { name: /\/help/ }));
    expect(screen.getByText('命令帮助')).toBeInTheDocument();
    expect(transport.requests.some((call) => call.request.pathId === 'agent.session.prompt')).toBe(false);

    await user.click(screen.getByRole('option', { name: /\/status/ }));
    expect(screen.getByLabelText('当前对话任务中心')).toHaveAttribute('data-open', 'true');

    await openCommandPalette();
    await user.click(screen.getByRole('option', { name: /\/settings/ }));
    expect(window.location.hash).toBe('#/configuration');
  });

  it('closes and disables the permission picker as soon as a turn becomes busy', async () => {
    const transport = featureTransport();
    const user = userEvent.setup();
    renderAgent(transport);

    const permissionButton = await screen.findByRole('button', { name: '对话权限：写入与命令确认' });
    await user.click(permissionButton);
    expect(await screen.findByText('对话权限')).toBeInTheDocument();

    act(() => {
      useAgentLiveStore.getState().appendOptimistic('session-preview', {
        clientMessageId: 'permission-busy-turn',
        text: '先完成当前回合',
        attachments: [],
        nowMs: Date.now(),
      });
    });

    await waitFor(() => expect(document.querySelector('.agent-picker-popover')).toBeNull());
    expect(permissionButton).toBeDisabled();
    expect(permissionButton).toHaveAttribute('title', '请先结束或停止当前任务，再调整运行权限。');
    expect(transport.requests.some((call) => call.request.pathId === 'agent.session.mode.update')).toBe(false);
  });

  it('offers full system access without a workspace prerequisite', async () => {
    const assistantSession = {
      ...previewSessions[0]!,
      mode: 'assistant' as const,
      workspaceRoots: [],
      toolProfileVersion: 'control-center-v1',
    };
    const transport = featureTransport(
      undefined,
      undefined,
      { ok: true, items: [assistantSession] },
    );
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole('button', { name: '对话权限：写入与命令确认' }));
    const permissionPicker = document.querySelector('.agent-picker-popover');
    expect(permissionPicker).not.toBeNull();
    await user.click(within(permissionPicker as HTMLElement).getByRole('radio', { name: /完全访问/ }));

    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'agent.session.mode.update',
        params: { sessionId: 'session-preview' },
        body: {
          mode: 'coordinator',
          executionMode: 'per_action',
          workspaceRoots: ['/'],
          toolProfileVersion: 'control-center-full-access-v1',
          toolAllowlistMode: 'profile',
        },
      }),
    })));
    expect(await screen.findByRole('button', { name: '对话权限：完全访问' })).toBeInTheDocument();
  });


  it('confirms full automation once without a checkbox or workspace gate', async () => {
    const assistantSession = {
      ...previewSessions[0]!,
      mode: 'assistant' as const,
      workspaceRoots: [],
      toolProfileVersion: 'control-center-v1',
    };
    const transport = featureTransport(
      undefined,
      undefined,
      { ok: true, items: [assistantSession] },
    );
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole('button', { name: '对话权限：写入与命令确认' }));
    const permissionPicker = document.querySelector('.agent-picker-popover');
    expect(permissionPicker).not.toBeNull();
    await user.click(within(permissionPicker as HTMLElement).getByRole('radio', { name: /全自动/ }));

    const dialog = await screen.findByRole('dialog', { name: '启用全自动模式？' });
    expect(within(dialog).queryByRole('checkbox')).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: '启用全自动' }));

    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'agent.session.mode.update',
        params: { sessionId: 'session-preview' },
        body: {
          mode: 'coordinator',
          executionMode: 'full_trust',
          workspaceRoots: ['/'],
          toolProfileVersion: 'control-center-auto-approve-v1',
          toolAllowlistMode: 'profile',
          dangerousModeConfirmation: 'ENABLE_FULL_TRUST',
        },
      }),
    })));
    expect(await screen.findByRole('button', { name: '对话权限：全自动' })).toBeInTheDocument();
  });

  it('keeps legacy workspace roots scoped and preserves their explicit allowlist', async () => {
    const legacySession = {
      ...previewSessions[0]!,
      mode: 'assistant' as const,
      executionMode: 'per_action' as const,
      toolProfileVersion: 'control-center-v1' as const,
      toolAllowlistMode: 'explicit' as const,
      allowedTools: ['overview', 'memory'],
      workspaceRoots: ['/Users/example/Projects/old'],
    };
    const transport = featureTransport(
      undefined,
      undefined,
      { ok: true, activeSessionId: 'session-preview', items: [legacySession] },
    );
    vi.spyOn(transport, 'pickFiles').mockResolvedValue([{
      id: 'workspace-root',
      name: 'new',
      mimeType: 'inode/directory',
      byteSize: 0,
      path: '/Users/example/Projects/new',
    }]);
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole('button', { name: '展开文件目录' }));
    const panel = screen.getByRole('complementary', { name: '当前对话文件目录' });
    await user.click(within(panel).getByRole('button', { name: '管理工作区目录' }));

    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'agent.session.mode.update',
        params: { sessionId: 'session-preview' },
        body: {
          mode: 'coordinator',
          executionMode: 'per_action',
          workspaceRoots: ['/Users/example/Projects/new'],
          toolProfileVersion: 'control-center-v1',
          toolAllowlistMode: 'explicit',
          allowedTools: ['overview', 'memory'],
        },
      }),
    })));
  });

  it('keeps read-only workspace roots scoped and preserves their explicit allowlist', async () => {
    const readonlySession = {
      ...previewSessions[0]!,
      mode: 'assistant' as const,
      executionMode: 'read_only' as const,
      toolProfileVersion: 'subagent-readonly-v1' as const,
      toolAllowlistMode: 'explicit' as const,
      allowedTools: ['memory'],
      workspaceRoots: ['/Users/example/Projects/old'],
    };
    const transport = featureTransport(
      undefined,
      undefined,
      { ok: true, activeSessionId: 'session-preview', items: [readonlySession] },
    );
    vi.spyOn(transport, 'pickFiles').mockResolvedValue([{
      id: 'workspace-root',
      name: 'new',
      mimeType: 'inode/directory',
      byteSize: 0,
      path: '/Users/example/Projects/new',
    }]);
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole('button', { name: '展开文件目录' }));
    const panel = screen.getByRole('complementary', { name: '当前对话文件目录' });
    await user.click(within(panel).getByRole('button', { name: '管理工作区目录' }));

    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'agent.session.mode.update',
        params: { sessionId: 'session-preview' },
        body: {
          mode: 'coordinator',
          executionMode: 'read_only',
          workspaceRoots: ['/Users/example/Projects/new'],
          toolProfileVersion: 'subagent-readonly-v1',
          toolAllowlistMode: 'explicit',
          allowedTools: ['memory'],
        },
      }),
    })));
  });

  it('confirms a replacement workspace for an existing managed Session', async () => {
    const managedSession = {
      ...previewSessions[0]!,
      mode: 'coordinator' as const,
      executionMode: 'workspace_managed' as const,
      toolProfileVersion: 'control-center-v1' as const,
      toolAllowlistMode: 'profile' as const,
      workspaceRoots: ['/Users/example/Projects/old'],
    };
    const transport = featureTransport(
      undefined,
      undefined,
      { ok: true, activeSessionId: 'session-preview', items: [managedSession] },
    );
    vi.spyOn(transport, 'pickFiles').mockResolvedValue([{
      id: 'workspace-root',
      name: 'new',
      mimeType: 'inode/directory',
      byteSize: 0,
      path: '/Users/example/Projects/new',
    }]);
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole('button', { name: '展开文件目录' }));
    const panel = screen.getByRole('complementary', { name: '当前对话文件目录' });
    await user.click(within(panel).getByRole('button', { name: '管理工作区目录' }));

    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'agent.session.mode.update',
        params: { sessionId: 'session-preview' },
        body: {
          mode: 'coordinator',
          executionMode: 'workspace_managed',
          workspaceRoots: ['/Users/example/Projects/new'],
          toolProfileVersion: 'control-center-v1',
          toolAllowlistMode: 'profile',
          workspaceScopeConfirmation: 'APPROVE_WORKSPACE_SCOPE',
        },
      }),
    })));
  });

  it('adds the system root and clears stale allowlists only for paired explicit profiles', async () => {
    const explicitSession = {
      ...previewSessions[0]!,
      mode: 'coordinator' as const,
      executionMode: 'per_action' as const,
      toolProfileVersion: 'control-center-full-access-v1' as const,
      toolAllowlistMode: 'explicit' as const,
      allowedTools: ['overview'],
      workspaceRoots: ['/Users/example/Projects/old'],
    };
    const transport = featureTransport(
      undefined,
      undefined,
      { ok: true, activeSessionId: 'session-preview', items: [explicitSession] },
    );
    vi.spyOn(transport, 'pickFiles').mockResolvedValue([{
      id: 'workspace-root',
      name: 'new',
      mimeType: 'inode/directory',
      byteSize: 0,
      path: '/Users/example/Projects/new',
    }]);
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole('button', { name: '展开文件目录' }));
    const panel = screen.getByRole('complementary', { name: '当前对话文件目录' });
    await user.click(within(panel).getByRole('button', { name: '管理工作区目录' }));

    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'agent.session.mode.update',
        params: { sessionId: 'session-preview' },
        body: {
          mode: 'coordinator',
          executionMode: 'per_action',
          workspaceRoots: ['/Users/example/Projects/new', '/'],
          toolProfileVersion: 'control-center-full-access-v1',
          toolAllowlistMode: 'profile',
        },
      }),
    })));
    const update = transport.requests.find((call) => call.request.pathId === 'agent.session.mode.update');
    expect(update?.request.body).not.toHaveProperty('allowedTools');
  });

  it('sends an advertised Pi RPC command through the prompt route', async () => {
    const transport = featureTransport();
    const user = userEvent.setup();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.session.commands')).toBe(true));

    await user.type(composer, '/rev');
    await user.keyboard('{Enter}');
    await user.type(composer, '本轮改动');
    await user.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'agent.session.prompt',
        body: expect.objectContaining({ message: '/review 本轮改动' }),
      }),
    })));
  });

  it('disables commands from live busy and permission state with visible reasons', async () => {
    const assistantSession = { ...previewSessions[0]!, mode: 'assistant' as const, workspaceRoots: [] };
    const coordinatorOnlyTools = toolCatalog().filter((tool) => tool.id.startsWith('workspace_'));
    const transport = featureTransport(
      undefined,
      { ok: true, items: coordinatorOnlyTools },
      { ok: true, items: [assistantSession] },
    );
    const user = userEvent.setup();
    renderAgent(transport);
    await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.session.commands')).toBe(true));

    const composer = screen.getByRole('textbox', { name: '消息' });
    await user.type(composer, '/');
    const toolsCommand = screen.getByRole('option', { name: /\/tools/ });
    expect(toolsCommand).toBeDisabled();
    expect(toolsCommand).toHaveAttribute('title', '写入与命令确认没有可用工具');
    await user.keyboard('{Escape}');

    act(() => {
      useAgentLiveStore.getState().appendOptimistic('session-preview', {
        clientMessageId: 'busy-command-state',
        text: '保持处理中',
        attachments: [],
        nowMs: Date.now(),
      });
    });
    await user.clear(composer);
    await user.type(composer, '/');
    const newCommand = screen.getByRole('option', { name: /\/new/ });
    expect(newCommand).toBeDisabled();
    expect(newCommand).toHaveAttribute('title', '当前处理中，仅可切换对话、查看状态或停止');
    expect(screen.getByRole('option', { name: /\/resume/ })).toBeEnabled();
    expect(screen.getByRole('option', { name: /\/status/ })).toBeEnabled();
    expect(screen.getByRole('option', { name: /\/subagents/ })).toBeEnabled();
    const stopCommand = screen.getByRole('option', { name: /\/stop/ });
    expect(stopCommand).toBeEnabled();
    await user.click(stopCommand);
    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.session.abort')).toBe(true));
    expect(transport.requests.some((call) => call.request.pathId === 'agent.session.prompt')).toBe(false);
  });

  it('accepts a planning handoff as an editable composer draft', async () => {
    const transport = featureTransport();
    renderAgent(transport, `/agent?draft=${encodeURIComponent('请整理今天的真实规划')}`);

    expect(await screen.findByRole('textbox', { name: '消息' })).toHaveValue('请整理今天的真实规划');
    expect(transport.requests.some((call) => call.request.pathId === 'agent.session.prompt')).toBe(false);
  });

  it('pastes supported clipboard images into the managed attachment chain', async () => {
    const transport = featureTransport();
    const user = userEvent.setup();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await screen.findByRole('button', { name: /模型与推理：GPT-5\.4/ }, { timeout: 15_000 });
    const image = new File([new Uint8Array([137, 80, 78, 71])], 'clipboard.png', { type: 'image/png' });

    fireEvent.paste(composer, {
      clipboardData: {
        files: [],
        items: [{ kind: 'file', getAsFile: () => image }],
      },
    });

    await waitFor(() => expect(transport.imagePasteCalls).toHaveLength(1));
    expect(transport.imagePasteCalls[0]).toMatchObject({ sessionId: 'session-preview' });
    expect(await screen.findByText('screen.png')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.session.prompt')).toBe(true));
    const prompt = transport.requests.find((call) => call.request.pathId === 'agent.session.prompt');
    expect(prompt?.request.body).toMatchObject({ attachments: ['media_fixture_attachment_01'] });
  });

  it('returns a late image import to its source Session instead of dropping or leaking it', async () => {
    const pendingPaste = deferred<Array<{
      id: string;
      name: string;
      mimeType: string;
      byteSize: number;
      sessionId: string;
      sha256: string;
    }>>();
    const transport = featureTransport();
    vi.spyOn(transport, 'pasteImages').mockImplementation(() => pendingPaste.promise);
    const user = userEvent.setup();
    renderAgent(transport);
    const composerA = await screen.findByRole('textbox', { name: '消息' });
    await screen.findByRole('button', { name: /模型与推理：GPT-5\.4/ }, { timeout: 15_000 });
    const image = new File([new Uint8Array([137, 80, 78, 71])], 'late.png', { type: 'image/png' });

    fireEvent.paste(composerA, {
      clipboardData: { files: [image], items: [] },
    });
    await waitFor(() => expect(transport.pasteImages).toHaveBeenCalledTimes(1));

    await user.click(screen.getByRole('button', { name: '记忆整理' }));
    const composerB = await screen.findByRole('textbox', { name: '消息' });
    await user.type(composerB, 'B 保留自己的草稿');
    await act(async () => pendingPaste.resolve([{
      id: 'media-late-a',
      name: 'late.png',
      mimeType: 'image/png',
      byteSize: 4,
      sessionId: 'session-preview',
      sha256: 'a'.repeat(64),
    }]));

    expect(composerB).toHaveValue('B 保留自己的草稿');
    expect(screen.queryByText('late.png')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '控制中心迁移' }));
    expect(await screen.findByText('late.png')).toBeInTheDocument();
  });

  it('imports a selected image through the native managed attachment picker', async () => {
    const transport = featureTransport();
    const user = userEvent.setup();
    renderAgent(transport);

    await screen.findByRole('button', { name: /模型与推理：GPT-5\.4/ }, { timeout: 15_000 });
    const attachmentButton = await screen.findByRole('button', { name: '添加内容' });
    expect(attachmentButton).toBeVisible();
    expect(attachmentButton).toBeEnabled();
    await user.click(attachmentButton);
    await user.click(screen.getByRole('menuitem', { name: /选择附件/ }));

    // No accepts filter: the picker takes any file, not only the image set.
    expect(transport.filePickCalls).toEqual([{
      multiple: true,
      purpose: 'attachment',
      sessionId: 'session-preview',
      maxFiles: 8,
    }]);
    expect(await screen.findByText('screen.png')).toBeInTheDocument();
  });

  it('leaves ordinary text paste alone, imports non-image files, and rejects oversized ones', async () => {
    const transport = featureTransport();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await screen.findByRole('button', { name: /模型与推理：GPT-5\.4/ }, { timeout: 15_000 });
    expect(fireEvent.paste(composer, { clipboardData: { files: [], getData: () => '普通文本' } })).toBe(true);
    expect(transport.imagePasteCalls).toHaveLength(0);

    // Non-image files ride the same managed import path as images now.
    const pastedDocument = new File(['{}'], 'notes.json', { type: 'application/json' });
    expect(fireEvent.paste(composer, { clipboardData: { files: [pastedDocument] } })).toBe(false);
    await waitFor(() => expect(transport.imagePasteCalls).toHaveLength(1));
    expect(transport.imagePasteCalls[0]).toMatchObject({ sessionId: 'session-preview', maxFiles: 1 });
    expect(transport.imagePasteCalls[0]?.files?.[0]?.name).toBe('notes.json');

    const oversized = new File(['x'], 'huge.webp', { type: 'image/webp' });
    Object.defineProperty(oversized, 'size', { value: 20 * 1024 * 1024 + 1 });
    fireEvent.paste(composer, { clipboardData: { files: [oversized] } });
    expect(await screen.findByRole('alert')).toHaveTextContent('必须小于 20 MiB');
    expect(transport.imagePasteCalls).toHaveLength(1);
  });

  it('asks the native host to read the system pasteboard when WebKit hides the image File', async () => {
    const transport = featureTransport();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await screen.findByRole('button', { name: /模型与推理：GPT-5\.4/ }, { timeout: 15_000 });
    expect(screen.getByRole('button', { name: '添加内容' })).toBeEnabled();

    expect(fireEvent.paste(composer, {
      clipboardData: {
        files: [],
        items: [{ kind: 'file', type: 'image/png', getAsFile: () => null }],
      },
    })).toBe(false);
    await waitFor(() => expect(transport.imagePasteCalls).toHaveLength(1));
    expect(transport.imagePasteCalls[0]).toEqual({ sessionId: 'session-preview', maxFiles: 8 });
  });

  it('falls back to the native pasteboard when WebKit exposes neither File nor item metadata', async () => {
    const transport = featureTransport();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await screen.findByRole('button', { name: /模型与推理：GPT-5\.4/ }, { timeout: 15_000 });

    expect(fireEvent.paste(composer, {
      clipboardData: { files: [], items: [], getData: () => '' },
    })).toBe(false);

    await waitFor(() => expect(transport.imagePasteCalls).toEqual([
      { sessionId: 'session-preview', maxFiles: 8 },
    ]));
  });

  it('keeps attachments open for a Pi text-only model and blocks image sends with an explanation', async () => {
    const catalog = previewModelCatalog('session-preview');
    catalog.selected = { provider: 'deepseek', id: 'deepseek-v4' };
    catalog.thinkingLevel = 'off';
    const transport = featureTransport(catalog);
    const user = userEvent.setup();
    renderAgent(transport);
    const composer = await screen.findByRole('textbox', { name: '消息' });

    const modelPicker = await screen.findByRole('button', {
      name: '模型与推理：DeepSeek V4 · DeepSeek · 不启用推理',
    });
    expect(modelPicker).toHaveTextContent('DeepSeek V4');
    expect(modelPicker).toHaveAccessibleName('模型与推理：DeepSeek V4 · DeepSeek · 不启用推理');
    // Documents still attach on a text-only model; only images are the problem.
    expect(screen.getByRole('button', { name: '添加内容' })).toBeEnabled();

    const image = new File(['png'], 'clipboard.png', { type: 'image/png' });
    expect(fireEvent.paste(composer, { clipboardData: { files: [image] } })).toBe(false);
    await waitFor(() => expect(transport.imagePasteCalls).toHaveLength(1));
    expect(await screen.findByText('screen.png')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '发送' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('当前模型不支持图片，请移除图片附件或切换到支持图片的模型。');
    expect(transport.requests.some((call) => call.request.pathId === 'agent.session.prompt')).toBe(false);
  });

  it('loads the complete tool catalog and writes an explicit tool intent without faking execution', async () => {
    const transport = featureTransport();
    const user = userEvent.setup();
    renderAgent(transport);
    const trigger = await screen.findByRole(
      'button',
      { name: '对话功能：记忆、工具、插件与技能；这段对话可执行工具：14 个；已登记工具：14 个' },
      { timeout: 5_000 },
    );

    await user.click(trigger);
    expect(screen.getByRole('button', { name: /^控制中心概览/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^受控命令/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^记忆召回/ }).closest('article')).toHaveTextContent(
      '把相关记忆加入对话，并允许 Agent 查询记忆。关闭后从下一轮停止使用；不会删除已保存的记忆。',
    );
    expect(screen.getByRole('button', { name: /^知识库 \/ Agent RAG/ }).closest('article')).toHaveTextContent(
      '启用后，Agent 可按当前问题反复检索已允许的知识库。',
    );
    await user.click(screen.getByRole('combobox', { name: '知识库 / Agent RAG的当前对话使用' }));
    await user.click(await screen.findByRole('option', { name: '当前对话关闭' }));
    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'agent.session.capability-policy.update',
        params: { sessionId: 'session-preview' },
        body: { capabilityDisclosurePreferences: { 'tool:knowledge': 'disabled' } },
      }),
    })));
    await user.click(screen.getByRole('button', { name: /^控制中心概览/ }));

    expect(screen.getByRole('textbox', { name: '消息' })).toHaveValue('帮我看看当前状态：');
    expect(screen.queryByText('overview')).not.toBeInTheDocument();
    expect(transport.requests.some((call) => call.request.pathId === 'agent.session.prompt')).toBe(false);
  });

  it('keeps Sessions and roles usable when the tool catalog is unavailable', async () => {
    const transport = featureTransport(previewModelCatalog('session-preview'), () => {
      throw new Error('tools unavailable');
    });
    renderAgent(transport);

    expect(await screen.findByRole('button', { name: '控制中心迁移' })).toBeInTheDocument();
    expect(await screen.findByRole('textbox', { name: '消息' })).toBeInTheDocument();
    const unavailableTools = await screen.findByRole('button', { name: '对话功能：记忆、工具、插件与技能；能力列表暂不可用' });
    expect(unavailableTools).toBeDisabled();
    expect(unavailableTools).toHaveTextContent('功能 · 未加载');
  });

  it('opens the backend active conversation instead of a newer empty Session', async () => {
    const emptySession = { ...previewSessions[0], id: 'session-empty', title: '刚创建的空对话', messageCount: 0, lastMessagePreview: '' };
    const activeSession = { ...previewSessions[1], id: 'session-active', title: '仍在继续的对话', messageCount: 12, lastMessagePreview: '最近的助手回复' };
    const transport = featureTransport(
      previewModelCatalog('session-active'),
      { ok: true, items: toolCatalog() },
      { ok: true, activeSessionId: activeSession.id, items: [emptySession, activeSession] },
    );
    renderAgent(transport);

    expect(await screen.findByRole('button', { name: '仍在继续的对话' })).toHaveAttribute('aria-current', 'true');
    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'agent.session.snapshot',
        params: { sessionId: 'session-active' },
      }),
    })));
  });

  it('does not let an empty runtime-active Session hide the most recent conversation', async () => {
    const emptyActive = { ...previewSessions[0], id: 'session-empty-active', title: '空白活动对话', messageCount: 0, lastMessagePreview: '' };
    const meaningful = { ...previewSessions[1], id: 'session-meaningful', title: '继续昨天的对话', messageCount: 6, lastMessagePreview: '已保留回复' };
    const transport = featureTransport(
      previewModelCatalog('session-meaningful'),
      { ok: true, items: toolCatalog() },
      { ok: true, activeSessionId: emptyActive.id, items: [emptyActive, meaningful] },
    );
    renderAgent(transport);

    expect(await screen.findByRole('button', { name: '继续昨天的对话' })).toHaveAttribute('aria-current', 'true');
  });

  it('updates the Session rail immediately and reconciles its preview after a terminal event', async () => {
    const initialSession = {
      ...previewSessions[0]!,
      messageCount: 0,
      lastMessagePreview: '',
    };
    let sessionListCalls = 0;
    const transport = featureTransport(
      undefined,
      undefined,
      () => {
        sessionListCalls += 1;
        return {
          ok: true,
          activeSessionId: initialSession.id,
          items: [{
            ...initialSession,
            ...(sessionListCalls > 1
              ? { messageCount: 6, lastMessagePreview: '权威助手摘要' }
              : {}),
          }],
        };
      },
    );
    const user = userEvent.setup();
    renderAgent(transport);

    const rail = await screen.findByLabelText('对话与项目');
    expect(within(rail).getByText('0 条消息')).toBeInTheDocument();
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await user.type(composer, '刚刚发送的用户请求');
    await user.click(screen.getByRole('button', { name: '发送' }));

    expect(within(rail).getByText('刚刚发送的用户请求')).toBeInTheDocument();
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));
    const projection = useAgentLiveStore.getState().projections['session-preview'];
    act(() => {
      transport.emit('agent.session.events', {
        schemaVersion: 'rag-ime.agent-event.v1',
        eventId: 'rail-summary-terminal',
        sessionId: 'session-preview',
        turnId: projection.turnOrder.at(-1) ?? 'turn-rail-summary',
        sequence: projection.lastSequence + 1,
        createdAtMs: Date.now(),
        streamKind: 'agent',
        eventType: 'turn_completed',
        payload: { status: 'completed', messageCount: 6 },
        resumeToken: `session-preview:${projection.lastSequence + 1}`,
      });
    });

    await waitFor(() => expect(sessionListCalls).toBe(2));
    expect(await within(rail).findByText('权威助手摘要')).toBeInTheDocument();
  });

  it('shows the conversation rail while the independent role catalog is slow', async () => {
    const pendingRoles = deferred<unknown>();
    const transport = productionTransport({
      'agent.roles.list': () => pendingRoles.promise,
    });
    renderAgent(transport);

    expect(await screen.findByRole(
      'button',
      { name: '控制中心迁移' },
      { timeout: 5_000 },
    )).toBeInTheDocument();
    expect(await screen.findByRole('textbox', { name: '消息' })).toBeEnabled();

    await act(async () => pendingRoles.resolve({ ok: true, items: previewPersonas }));
  });

  it('keeps persisted conversation visible when the model catalog fails', async () => {
    const transport = productionTransport({
      'agent.session.models': () => { throw new Error('provider probe failed'); },
    });
    renderAgent(transport);

    await waitFor(() => expect(useAgentLiveStore.getState().projections['session-preview']?.messageOrder).toHaveLength(4));
    expect(useAgentLiveStore.getState().projections['session-preview']?.turnOrder).toHaveLength(2);
    expect(await screen.findByRole('alert')).toHaveTextContent('模型目录暂时不可用，对话记录仍可查看');
  });

  it('clears only the recovered catalog warning after a Session retry succeeds', async () => {
    const first = { ...previewSessions[0]!, id: 'session-catalog-a', title: '目录恢复 A' };
    const second = { ...previewSessions[0]!, id: 'session-catalog-b', title: '目录恢复 B' };
    let firstModelCalls = 0;
    const transport = productionTransport({
      'agent.sessions.list': { ok: true, activeSessionId: first.id, items: [first, second] },
      'agent.session.snapshot': (request: ControlRequest) => previewAgentSnapshot(request.params?.sessionId ?? first.id),
      'agent.session.models': (request: ControlRequest) => {
        const sessionId = request.params?.sessionId ?? first.id;
        if (sessionId === first.id) {
          firstModelCalls += 1;
          if (firstModelCalls === 1) throw new Error('temporary model discovery failure');
        }
        return { ...previewModelCatalog(sessionId), sessionId };
      },
    });
    const user = userEvent.setup();
    renderAgent(transport);

    expect(await screen.findByRole('alert')).toHaveTextContent('模型目录暂时不可用');
    await user.click(screen.getByRole('button', { name: '目录恢复 B' }));
    await waitFor(() => expect(transport.requests.some((request) => (
      request.pathId === 'agent.session.models'
      && request.params?.sessionId === second.id
    ))).toBe(true));
    await user.click(screen.getByRole('button', { name: '目录恢复 A' }));
    await waitFor(() => expect(firstModelCalls).toBe(2));
    await waitFor(() => expect(screen.queryByText(/模型目录暂时不可用/)).not.toBeInTheDocument());
  });

  it('keeps the last Pi-confirmed model catalog visible while a returning Session refreshes', async () => {
    const user = userEvent.setup();
    const returningRefresh = deferred<ModelCatalog>();
    const memoryCatalog = previewModelCatalog('session-memory');
    memoryCatalog.selected = { provider: 'deepseek', id: 'deepseek-v4' };
    memoryCatalog.thinkingLevel = 'off';
    let previewCatalogRequests = 0;
    const transport = productionTransport({
      'agent.session.models': (request: ControlRequest) => {
        const sessionId = String(request.params?.sessionId ?? '');
        if (sessionId === 'session-preview') {
          previewCatalogRequests += 1;
          return previewCatalogRequests === 1
            ? previewModelCatalog(sessionId)
            : returningRefresh.promise;
        }
        return memoryCatalog;
      },
      'agent.session.snapshot': (request: ControlRequest) => (
        previewAgentSnapshot(String(request.params?.sessionId ?? 'session-preview'))
      ),
    });
    renderAgent(transport);

    await screen.findByRole(
      'button',
      { name: /模型与推理：GPT-5\.4/ },
      { timeout: 15_000 },
    );
    await user.click(screen.getByRole('button', { name: '记忆整理' }));
    await screen.findByRole(
      'button',
      { name: /模型与推理：DeepSeek V4/ },
      { timeout: 15_000 },
    );

    await user.click(screen.getByRole('button', { name: '控制中心迁移' }));
    await waitFor(() => expect(
      screen.getByRole('button', { name: '控制中心迁移' }),
    ).toHaveAttribute('aria-current', 'true'));
    expect(screen.getByRole('button', { name: /模型与推理：GPT-5\.4/ })).toBeEnabled();

    await act(async () => returningRefresh.reject(new Error('provider probe failed')));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      '模型目录刷新失败，正在继续使用这段对话上次由 Pi 确认的模型状态',
    );
    expect(screen.getByRole('button', { name: /模型与推理：GPT-5\.4/ })).toBeEnabled();
  });

  it('identifies an expired Session workspace separately from Provider availability', async () => {
    const transport = productionTransport({
      'agent.session.models': () => {
        throw new Error('session runtime is unavailable because its workspace no longer exists');
      },
    });
    renderAgent(transport);

    await waitFor(() => expect(useAgentLiveStore.getState().projections['session-preview']?.messageOrder).toHaveLength(4));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      '这段对话的工作目录已不可用；对话记录仍保留，可以归档后选择其他对话',
    );
  });

  it('makes tools ready without waiting for slow model and command catalogs', async () => {
    const pendingModels = deferred<unknown>();
    const pendingCommands = deferred<unknown>();
    const transport = productionTransport({
      'agent.session.models': () => pendingModels.promise,
      'agent.session.commands': () => pendingCommands.promise,
    });
    renderAgent(transport);

    expect(await screen.findByRole(
      'button',
      { name: /这段对话可执行工具：14 个；已登记工具：14 个/ },
      { timeout: 5_000 },
    )).toBeEnabled();

    await act(async () => {
      pendingModels.resolve(previewModelCatalog('session-preview'));
      pendingCommands.resolve(commandCatalog());
    });
  });

  it('makes the model and tool controls ready while a slow history snapshot is still loading', async () => {
    const pendingSnapshot = deferred<unknown>();
    const transport = productionTransport({
      'agent.session.snapshot': () => pendingSnapshot.promise,
    });
    renderAgent(transport);

    expect(await screen.findByRole(
      'button',
      { name: /模型与推理：GPT-5\.4/ },
      { timeout: 15_000 },
    )).toBeEnabled();
    expect(await screen.findByRole(
      'button',
      { name: /这段对话可执行工具：14 个；已登记工具：14 个/ },
      { timeout: 5_000 },
    )).toBeEnabled();
    expect(useAgentLiveStore.getState().projections['session-preview']?.messageOrder ?? []).toEqual([]);

    pendingSnapshot.resolve(previewAgentSnapshot('session-preview'));
    await waitFor(() => expect(
      useAgentLiveStore.getState().projections['session-preview']?.messageOrder,
    ).toHaveLength(4));
  });

  it('keeps persisted conversation visible when the Pi command catalog fails', async () => {
    const transport = productionTransport({
      'agent.session.commands': () => { throw new Error('commands unavailable'); },
    });
    renderAgent(transport);

    await waitFor(() => expect(useAgentLiveStore.getState().projections['session-preview']?.messageOrder).toHaveLength(4));
    expect(useAgentLiveStore.getState().projections['session-preview']?.turnOrder).toHaveLength(2);
    expect(await screen.findByRole('alert')).toHaveTextContent('Pi 命令暂时不可用，仍可直接发送消息');
  });

  it('rehydrates persisted user and assistant blocks after a page remount', async () => {
    const first = productionTransport();
    const rendered = renderAgent(first);
    await waitFor(() => expect(useAgentLiveStore.getState().projections['session-preview']?.messageOrder).toHaveLength(4));

    rendered.unmount();
    useAgentLiveStore.getState().clear('session-preview');
    renderAgent(productionTransport());

    await waitFor(() => expect(useAgentLiveStore.getState().projections['session-preview']?.messageOrder).toHaveLength(4));
    const projection = useAgentLiveStore.getState().projections['session-preview'];
    const persistedMessages = Object.values(projection?.messagesById ?? {});
    expect(persistedMessages.some((message) => message.role === 'user'
      && message.blocks.some((block) => String(block.data.text).includes('把迁移进度按真实代码链整理一下')))).toBe(true);
    expect(persistedMessages.some((message) => message.role === 'assistant'
      && message.blocks.some((block) => String(block.data.text).includes('三条工作线已经收束到同一个')))).toBe(true);
  });

  it('uses the Pi runtime selection without deriving the Persona from the model name', async () => {
    const transport = featureTransport(lunaModelCatalog());
    const user = userEvent.setup();
    renderAgent(transport);

    expect(await screen.findByRole(
      'button',
      { name: /模型与推理：GPT-5.6 Luna/ },
      { timeout: 15_000 },
    )).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '新建对话' }));
    const dialog = await screen.findByRole('dialog', { name: '新建对话' });
    expect(within(dialog).getByRole('radio', { name: /不预选项目/ })).toBeChecked();
    await user.click(within(dialog).getByRole('button', { name: '开始对话' }));
    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.sessions.create')).toBe(true));
    const create = transport.requests.find((call) => call.request.pathId === 'agent.sessions.create');
    expect(create?.request.body).toMatchObject({
      mode: 'coordinator',
      executionMode: 'per_action',
      toolProfileVersion: 'control-center-full-access-v1',
      workspaceRoots: ['/'],
    });
    expect(create?.request.body).not.toHaveProperty('roleId');
    expect(create?.request.body).not.toHaveProperty('roleVersion');
    expect(create?.request.body).not.toHaveProperty('modelProfile');
    expect(transport.requests.some((call) => call.request.pathId === 'agent.session.model.select')).toBe(false);
  });

  it('sends the explicit full-automation confirmation when creating a project conversation', async () => {
    const transport = featureTransport();
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole('button', { name: '新建对话' }));
    const dialog = await screen.findByRole('dialog', { name: '新建对话' });
    await user.click(within(dialog).getByRole('radio', { name: /personal-agent-workbench/ }));
    await user.click(within(dialog).getByRole('radio', { name: /全自动/ }));
    await user.click(within(dialog).getByRole('button', { name: '启用全自动并开始' }));

    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.sessions.create')).toBe(true));
    const create = transport.requests.find((call) => call.request.pathId === 'agent.sessions.create');
    expect(create?.request.body).toMatchObject({
      mode: 'coordinator',
      executionMode: 'full_trust',
      toolProfileVersion: 'control-center-auto-approve-v1',
      workspaceRoots: ['/Users/example/Projects/personal-agent-workbench', '/'],
      dangerousModeConfirmation: 'ENABLE_FULL_TRUST',
    });
  });

  it('keeps ordinary Sessions neutral when Persona and Subagent Packages are absent', async () => {
    const transport = productionTransport({
      'agent.session.commands': {
        schemaVersion: 'rag-ime.agent-command-catalog.v1',
        ok: true,
        sessionId: 'session-preview',
        runtimeAvailable: true,
        items: [{ name: 'review', invocation: '/review', description: '审阅当前改动', source: 'extension' }],
      },
    });
    const user = userEvent.setup();
    renderAgent(transport);

    await screen.findByRole('textbox', { name: '消息' });
    expect(screen.queryByRole('img', { name: 'Pi Agent' })).not.toBeInTheDocument();
    expect(document.querySelector('.agent-persona-avatar')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '打开子 Agent 工作台' })).not.toBeInTheDocument();
    const composer = screen.getByRole('textbox', { name: '消息' });
    expect(composer).toHaveAttribute('placeholder', expect.stringContaining('给当前 Session发消息'));
    await user.type(composer, '/');
    expect(screen.queryByRole('option', { name: /\/subagents/ })).not.toBeInTheDocument();
  });

  it('renders Pi-provided Max reasoning and sends max without inventing levels', async () => {
    const transport = featureTransport(lunaModelCatalog());
    const user = userEvent.setup();
    renderAgent(transport);

    await screen.findByRole('button', { name: /模型与推理：GPT-5.6 Luna/ }, { timeout: 15_000 });
    await user.click(screen.getByRole('button', { name: /模型与推理：GPT-5.6 Luna/ }));
    const picker = screen.getByRole('dialog', { name: '选择模型与推理强度' });
    for (const level of ['不启用推理', '轻量', '低', '中', '高', '极高', '最高']) {
      expect(within(picker).getByRole('radio', { name: level })).toBeInTheDocument();
    }
    const max = within(picker).getByRole('radio', { name: '最高' });
    await user.click(max);

    await waitFor(() => expect(transport.requests.some((call) => (
      call.request.pathId === 'agent.session.thinking.select'
      && typeof call.request.body === 'object'
      && call.request.body !== null
      && !Array.isArray(call.request.body)
      && call.request.body.level === 'max'
    ))).toBe(true));
  });

  it('lists every Provider group in one searchable model panel', async () => {
    const transport = featureTransport(previewModelCatalog('session-preview'));
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole(
      'button',
      { name: /模型与推理：GPT-5\.4/ },
      { timeout: 15_000 },
    ));
    const picker = screen.getByRole('dialog', { name: '选择模型与推理强度' });

    expect(within(picker).queryByRole('listbox')).not.toBeInTheDocument();
    await user.click(within(picker).getByRole('button', { name: /更换模型/ }));
    expect(within(picker).getByRole('searchbox', { name: '搜索模型' })).toBeInTheDocument();
    expect(within(picker).getByRole('group', { name: 'OpenAI' })).toBeInTheDocument();
    expect(within(picker).getByRole('group', { name: 'DeepSeek' })).toBeInTheDocument();
    expect(within(picker).getByRole('option', {
      name: '选择模型 GPT-5.4',
    })).toBeInTheDocument();
    expect(within(picker).getByRole('option', {
      name: '选择模型 DeepSeek V4',
    })).toBeInTheDocument();
    expect(within(picker).getByRole('radiogroup', { name: '推理强度' })).toBeInTheDocument();
    expect(within(picker).queryByRole('tab')).not.toBeInTheDocument();
  });

  it('keeps model search and reasoning keyboard interactions working in the merged popover', async () => {
    const initial = lunaModelCatalog();
    const confirmed = { ...initial, thinkingLevel: 'high' as ThinkingLevel };
    let catalogCalls = 0;
    const transport = featureTransport(() => {
      catalogCalls += 1;
      return catalogCalls === 1 ? initial : confirmed;
    });
    const user = userEvent.setup();
    renderAgent(transport);

    const trigger = await screen.findByRole(
      'button',
      { name: /模型与推理：GPT-5.6 Luna/ },
      { timeout: 15_000 },
    );
    await user.click(trigger);
    let picker = screen.getByRole('dialog', { name: '选择模型与推理强度' });
    await waitFor(() => expect(within(picker).getByRole('radio', { name: '中' })).toHaveFocus());
    expect(within(picker).queryByRole('searchbox', { name: '搜索模型' })).not.toBeInTheDocument();
    await user.click(within(picker).getByRole('button', { name: /更换模型/ }));
    const search = within(picker).getByRole('searchbox', { name: '搜索模型' });
    await waitFor(() => expect(search).toHaveFocus());
    await user.type(search, 'codex');
    expect(within(picker).getByRole('option', { name: '选择模型 Codex Mini' })).toBeInTheDocument();
    expect(within(picker).queryByRole('option', { name: '选择模型 GPT-5.6 Luna' })).not.toBeInTheDocument();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(picker).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();

    await user.click(trigger);
    picker = screen.getByRole('dialog', { name: '选择模型与推理强度' });
    within(picker).getByRole('radio', { name: '中' }).focus();
    await user.keyboard('{ArrowRight}{Enter}');

    await waitFor(() => expect(picker).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
    await waitFor(() => expect(screen.getByRole('button', {
      name: '模型与推理：GPT-5.6 Luna · GPT · 高',
    })).toBeInTheDocument());
  });

  it('switches models from the model row without requiring a reasoning-level click', async () => {
    const pendingSelection = deferred<{ ok: true }>();
    const initial = lunaModelCatalog();
    const confirmed = {
      ...initial,
      selected: {
        provider: 'gpt',
        id: 'codex-mini-latest',
        modelId: 'codex-mini-latest',
        name: 'Codex Mini',
      },
      thinkingLevel: 'medium' as ThinkingLevel,
    };
    let modelCatalogCalls = 0;
    const transport = featureTransport(
      () => {
        modelCatalogCalls += 1;
        return modelCatalogCalls === 1 ? initial : confirmed;
      },
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      () => pendingSelection.promise,
    );
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole(
      'button',
      { name: /模型与推理：GPT-5.6 Luna/ },
      { timeout: 15_000 },
    ));
    await user.click(within(screen.getByRole('dialog', { name: '选择模型与推理强度' }))
      .getByRole('button', { name: /更换模型/ }));
    await user.click(screen.getByLabelText('选择模型 Codex Mini'));

    expect(transport.requests.map((call) => call.request.pathId)).toContain(
      'agent.session.model.select',
    );
    expect(screen.queryByRole('dialog', { name: '选择模型与推理强度' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', {
      name: '模型与推理：Codex Mini · GPT · 中',
    })).toHaveAttribute('aria-busy', 'true');
    await waitFor(() => expect(transport.requests).toEqual(expect.arrayContaining([
      expect.objectContaining({ request: expect.objectContaining({
        pathId: 'agent.session.model.select',
        body: { provider: 'gpt', modelId: 'codex-mini-latest' },
      }) }),
    ])));
    pendingSelection.resolve({ ok: true });
    await waitFor(() => expect(transport.requests).toEqual(expect.arrayContaining([
      expect.objectContaining({ request: expect.objectContaining({
        pathId: 'agent.session.thinking.select',
        body: { level: 'medium' },
      }) }),
    ])));
    await waitFor(() => expect(screen.getByRole('button', {
      name: '模型与推理：Codex Mini · GPT · 中',
    })).not.toHaveAttribute('aria-busy'));
  });

  it('does not bottom-pin a short conversation before it fills the viewport', async () => {
    renderAgent(productionTransport());
    const timeline = await screen.findByTestId('agent-virtuoso');
    expect(timeline).not.toHaveAttribute(
      'data-align-to-bottom',
    );
    expect(timeline).toHaveAttribute('data-initial-align', 'start');
  });

  it('does not show Max when the Pi model catalog omits max', async () => {
    const catalog = lunaModelCatalog();
    catalog.providers[0]!.models[1] = modelFixture(
      'gpt-5.6-luna',
      'GPT-5.6 Luna',
      ['off', 'minimal', 'low', 'medium', 'high'],
    );
    const user = userEvent.setup();
    renderAgent(featureTransport(catalog));

    await screen.findByRole('button', { name: /模型与推理：GPT-5.6 Luna/ }, { timeout: 15_000 });
    await user.click(screen.getByRole('button', { name: /模型与推理：GPT-5.6 Luna/ }));
    const picker = screen.getByRole('dialog', { name: '选择模型与推理强度' });
    expect(within(picker).getAllByRole('radio')).toHaveLength(5);
    expect(within(picker).queryByRole('radio', { name: '最高' })).not.toBeInTheDocument();
  });

  it('uses the real model and thinking selection routes then reloads Pi state', async () => {
    const transport = featureTransport(lunaModelCatalog());
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole(
      'button',
      { name: /模型与推理：GPT-5.6 Luna/ },
      { timeout: 15_000 },
    ));
    await user.click(within(screen.getByRole('dialog', { name: '选择模型与推理强度' }))
      .getByRole('button', { name: /更换模型/ }));
    await user.click(screen.getByRole('option', { name: '选择模型 Codex Mini' }));

    await waitFor(() => expect(transport.requests).toEqual(expect.arrayContaining([
      expect.objectContaining({ request: expect.objectContaining({
        pathId: 'agent.session.model.select',
        body: { provider: 'gpt', modelId: 'codex-mini-latest' },
      }) }),
      expect.objectContaining({ request: expect.objectContaining({
        pathId: 'agent.session.thinking.select',
        body: { level: 'medium' },
      }) }),
    ])));
    expect(transport.requests.filter((call) => call.request.pathId === 'agent.session.models')).toHaveLength(2);
  });

  it('closes the model menu and reflects the choice before Pi confirms it', async () => {
    const pendingSelection = deferred<{ ok: true }>();
    const initial = lunaModelCatalog();
    const confirmed = {
      ...initial,
      selected: {
        provider: 'gpt',
        id: 'codex-mini-latest',
        modelId: 'codex-mini-latest',
        name: 'Codex Mini',
      },
      thinkingLevel: 'medium' as ThinkingLevel,
    };
    let modelCatalogCalls = 0;
    const transport = featureTransport(
      () => {
        modelCatalogCalls += 1;
        return modelCatalogCalls === 1 ? initial : confirmed;
      },
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      () => pendingSelection.promise,
    );
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole(
      'button',
      { name: /模型与推理：GPT-5.6 Luna/ },
      { timeout: 15_000 },
    ));
    await user.click(within(screen.getByRole('dialog', { name: '选择模型与推理强度' }))
      .getByRole('button', { name: /更换模型/ }));
    await user.click(screen.getByRole('option', { name: '选择模型 Codex Mini' }));

    expect(screen.queryByRole('dialog', { name: '选择模型与推理强度' })).not.toBeInTheDocument();
    const optimistic = screen.getByRole('button', {
      name: '模型与推理：Codex Mini · GPT · 中',
    });
    expect(optimistic).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('textbox', { name: '消息' })).toBeEnabled();

    pendingSelection.resolve({ ok: true });
    await waitFor(() => expect(optimistic).not.toHaveAttribute('aria-busy'));
    expect(modelCatalogCalls).toBe(2);
  });

  it('coalesces rapid model changes to the latest visible choice', async () => {
    const firstSelection = deferred<{ ok: true }>();
    const initial = lunaModelCatalog();
    const confirmedCodex = {
      ...initial,
      selected: {
        provider: 'gpt',
        id: 'codex-mini-latest',
        modelId: 'codex-mini-latest',
        name: 'Codex Mini',
      },
      thinkingLevel: 'medium' as ThinkingLevel,
    };
    const confirmedLuna = {
      ...initial,
      thinkingLevel: 'high' as ThinkingLevel,
    };
    let modelCatalogCalls = 0;
    let modelSelectionCalls = 0;
    const transport = featureTransport(
      () => {
        modelCatalogCalls += 1;
        if (modelCatalogCalls === 1) return initial;
        return modelCatalogCalls === 2 ? confirmedCodex : confirmedLuna;
      },
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      () => {
        modelSelectionCalls += 1;
        return modelSelectionCalls === 1 ? firstSelection.promise : { ok: true };
      },
    );
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole(
      'button',
      { name: /模型与推理：GPT-5.6 Luna/ },
      { timeout: 15_000 },
    ));
    await user.click(within(screen.getByRole('dialog', { name: '选择模型与推理强度' }))
      .getByRole('button', { name: /更换模型/ }));
    await user.click(screen.getByRole('option', { name: '选择模型 Codex Mini' }));

    await user.click(screen.getByRole('button', { name: '模型与推理：Codex Mini · GPT · 中' }));
    await user.click(within(screen.getByRole('dialog', { name: '选择模型与推理强度' }))
      .getByRole('button', { name: /更换模型/ }));
    await user.click(screen.getByRole('option', { name: '选择模型 GPT-5.6 Luna' }));
    await user.click(screen.getByRole('button', { name: '模型与推理：GPT-5.6 Luna · GPT · 中' }));
    const picker = screen.getByRole('dialog', { name: '选择模型与推理强度' });
    await user.click(within(picker).getByRole('radio', { name: '高' }));
    expect(screen.getByRole('button', {
      name: '模型与推理：GPT-5.6 Luna · GPT · 高',
    })).toHaveAttribute('aria-busy', 'true');

    firstSelection.resolve({ ok: true });
    await waitFor(() => expect(modelSelectionCalls).toBe(2));
    await waitFor(() => expect(screen.getByRole('button', {
      name: '模型与推理：GPT-5.6 Luna · GPT · 高',
    })).not.toHaveAttribute('aria-busy'));
    expect(transport.requests.filter((call) => (
      call.request.pathId === 'agent.session.model.select'
    )).map((call) => call.request.body)).toEqual([
      { provider: 'gpt', modelId: 'codex-mini-latest' },
      { provider: 'gpt', modelId: 'gpt-5.6-luna' },
    ]);
  });

  it('applies Pi model configuration events without reloading every catalog', async () => {
    let modelCatalogCalls = 0;
    const transport = featureTransport(() => {
      modelCatalogCalls += 1;
      return lunaModelCatalog();
    });
    renderAgent(transport);

    await waitFor(() => expect(modelCatalogCalls).toBe(1));
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));
    const projection = useAgentLiveStore.getState().projections['session-preview'];
    const sequence = projection.lastSequence + 1;
    act(() => {
      transport.emit('agent.session.events', {
        schemaVersion: 'rag-ime.agent-event.v1',
        eventId: 'event-model-refresh',
        sessionId: 'session-preview',
        turnId: '',
        sequence,
        createdAtMs: Date.now(),
        streamKind: 'agent',
        eventType: 'session_configuration_changed',
        payload: {
          kind: 'thinking',
          thinkingLevel: 'high',
          selected: {
            provider: 'gpt',
            id: 'gpt-5.6-luna',
            modelId: 'gpt-5.6-luna',
            name: 'GPT-5.6 Luna',
          },
        },
        resumeToken: `session-preview:${sequence}`,
      });
    });

    expect(await screen.findByRole('button', {
      name: '模型与推理：GPT-5.6 Luna · GPT · 高',
    })).toBeInTheDocument();
    expect(modelCatalogCalls).toBe(1);
  });

  it('treats the mobile session rail as a focus-managed drawer', async () => {
    vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
      matches: query.includes('max-width: 760px') || query.includes('max-width: 1360px'),
    })));
    vi.stubGlobal('ResizeObserver', class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    const user = userEvent.setup();
    renderAgent(featureTransport());
    const feature = () => document.querySelector('.agent-feature');
    expect(feature()).toHaveAttribute('data-rail-open', 'false');

    const toggle = await screen.findByRole('button', { name: '展开对话列表' });
    await user.click(toggle);
    expect(feature()).toHaveAttribute('data-rail-open', 'true');
    const rail = screen.getByRole('dialog', { name: '对话与项目' });
    const conversation = document.querySelector('.agent-conversation');
    expect(rail).toHaveAttribute('aria-modal', 'true');
    expect(conversation).toHaveAttribute('inert');
    expect(conversation).toHaveAttribute('aria-hidden', 'true');
    await waitFor(() => expect(screen.getByPlaceholderText('搜索对话或项目')).toHaveFocus());

    const sessionRows = rail.querySelectorAll<HTMLButtonElement>('.agent-session-row');
    const lastSession = sessionRows.item(sessionRows.length - 1);
    const lastSessionArchive = lastSession.parentElement?.querySelector<HTMLButtonElement>('.agent-session-row__archive');
    const lastSessionMenu = lastSession.parentElement?.querySelector<HTMLButtonElement>('.agent-session-row__menu');
    expect(lastSessionArchive).toBeNull();
    expect(lastSessionMenu).not.toBeNull();
    lastSession.focus();
    await user.tab();
    expect(lastSessionMenu).toHaveFocus();
    await user.tab();
    expect(within(rail).getByRole('button', { name: '新建对话' })).toHaveFocus();
    await user.tab({ shift: true });
    expect(lastSessionMenu).toHaveFocus();
    await user.tab({ shift: true });
    expect(lastSession).toHaveFocus();

    await user.keyboard('{Escape}');
    expect(feature()).toHaveAttribute('data-rail-open', 'false');
    expect(conversation).not.toHaveAttribute('inert');
    expect(conversation).not.toHaveAttribute('aria-hidden');
    await waitFor(() => expect(toggle).toHaveFocus());

    await user.click(toggle);
    await user.click(within(rail).getByRole('button', { name: '新建对话' }));
    expect(feature()).toHaveAttribute('data-rail-open', 'false');
    expect(screen.getByRole('dialog', { name: '新建对话' })).toBeInTheDocument();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '新建对话' })).not.toBeInTheDocument());

    await user.click(toggle);
    expect(document.querySelector('.agent-rail-backdrop')).toBeInTheDocument();
    await user.click(await screen.findByRole('button', { name: '记忆整理' }));
    expect(feature()).toHaveAttribute('data-rail-open', 'false');
  });

  it('opens the authoritative responsive status panel from the compact header trigger', async () => {
    vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
      matches: query.includes('max-width: 1360px'),
    })));
    vi.stubGlobal('ResizeObserver', class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    const user = userEvent.setup();
    renderAgent(featureTransport());

    const toggle = await screen.findByRole('button', { name: '展开任务中心' });
    expect(screen.queryByRole('button', { name: /打开当前对话任务中心/ })).not.toBeInTheDocument();
    await user.click(toggle);
    const panel = screen.getByRole('dialog', { name: '当前对话任务中心' });
    const conversation = document.querySelector('.agent-conversation');
    const rail = document.querySelector('.agent-session-rail');
    expect(panel).toHaveAttribute('aria-modal', 'true');
    expect(conversation).toHaveAttribute('inert');
    expect(rail).toHaveAttribute('inert');
    await waitFor(() => expect(within(panel).getByRole('button', { name: '收起任务中心' })).toHaveFocus());

    await user.tab({ shift: true });
    expect(panel).toContainElement(document.activeElement as HTMLElement);
    expect(conversation).not.toContainElement(document.activeElement as HTMLElement);

    await user.keyboard('{Escape}');
    expect(panel).toHaveAttribute('aria-hidden', 'true');
    expect(conversation).not.toHaveAttribute('inert');
    expect(rail).not.toHaveAttribute('inert');
    await waitFor(() => expect(toggle).toHaveFocus());
  });

  it('opens a lazy workspace tree from the header and previews text files with the shared renderer', async () => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false })));
    const root = '/Users/example/Projects/personal-agent-workbench';
    const transport = productionTransport({
      'agent.session.workspace.list': (request: ControlRequest) => {
        const path = String(request.query?.path ?? '');
        return {
          schemaVersion: 'rag-ime.agent-workspace-list.v1',
          ok: true,
          sessionId: 'session-preview',
          root,
          path,
          items: path === root
            ? [
                { path: `${root}/src`, name: 'src', kind: 'directory' },
                { path: `${root}/README.md`, name: 'README.md', kind: 'file', byteSize: 18 },
              ]
            : [{ path: `${root}/src/index.ts`, name: 'index.ts', kind: 'file', byteSize: 24 }],
          truncated: false,
        };
      },
      'agent.session.workspace.read': (request: ControlRequest) => ({
        schemaVersion: 'rag-ime.agent-workspace-read.v1',
        ok: true,
        sessionId: 'session-preview',
        path: request.query?.path,
        root,
        content: '# Agent workspace\n\nRendered markdown.\n',
        byteSize: 39,
        truncated: false,
      }),
    });
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole('button', { name: '展开文件目录' }));
    const panel = screen.getByRole('complementary', { name: '当前对话文件目录' });
    expect(panel).toHaveAttribute('data-open', 'true');
    expect(screen.getByLabelText('当前对话任务中心')).toHaveAttribute('aria-hidden', 'true');
    expect(await within(panel).findByRole('treeitem', { name: '预览文件 README.md' })).toBeVisible();
    expect(transport.requests).toContainEqual(expect.objectContaining({
      pathId: 'agent.session.workspace.list',
      query: { path: root, depth: 1, limit: 240 },
    }));

    await user.click(within(panel).getByRole('treeitem', { name: '展开目录 src' }));
    expect(await within(panel).findByRole('treeitem', { name: '预览文件 index.ts' })).toBeVisible();

    await user.click(within(panel).getByRole('treeitem', { name: '预览文件 README.md' }));
    const preview = await screen.findByRole('dialog', { name: 'README.md' });
    expect(within(preview).getByRole('heading', { name: 'Agent workspace' })).toBeVisible();
    expect(transport.requests).toContainEqual(expect.objectContaining({
      pathId: 'agent.session.workspace.read',
      query: { path: `${root}/README.md`, offset: 0, limit: 65_536 },
    }));
  });

  it('opens the task center by default on wide desktops', async () => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false })));
    renderAgent(featureTransport());

    const panel = await screen.findByLabelText('当前对话任务中心');
    expect(panel).toHaveAttribute('data-open', 'true');
    expect(screen.getByRole('button', { name: '收起任务中心', expanded: true })).toBeInTheDocument();
  });

  it('releases the session rail grid column when collapsed on desktop', async () => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false })));
    const user = userEvent.setup();
    const { container } = render(
      <MemoryRouter initialEntries={['/agent']}>
        <ControlTransportProvider transport={featureTransport()}>
          <QueryClientProvider client={testQueryClient()}>
            <TooltipProvider>
              <div className="shell-route-stage"><AgentFeature /></div>
            </TooltipProvider>
          </QueryClientProvider>
        </ControlTransportProvider>
      </MemoryRouter>,
    );
    const feature = () => container.querySelector<HTMLElement>('main.agent-feature');
    expect(feature()).toHaveAttribute('data-rail-open', 'true');
    const composer = await screen.findByRole('textbox', { name: '消息' });
    expect(composer).toBeVisible();

    await user.click(screen.getByRole('button', { name: '收起对话列表' }));

    expect(feature()).toHaveAttribute('data-rail-open', 'false');
    expect(getComputedStyle(feature()!).gridTemplateColumns).toBe('0 minmax(0, 1fr) var(--agent-status-width)');
    expect(screen.getByRole('textbox', { name: '消息' })).toBe(composer);
    expect(composer).toBeVisible();
    expect(composer.closest('.agent-composer-wrap')).toBeInTheDocument();
  });

  it('selects the Session requested by the roles route handoff', async () => {
    const transport = featureTransport();
    renderAgent(transport, '/agent?session=session-memory');

    expect(await screen.findByRole('button', { name: '记忆整理' })).toHaveAttribute('aria-current', 'true');
    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'agent.session.snapshot',
        params: { sessionId: 'session-memory' },
      }),
    })));
  });

  it('does not substitute preview Sessions or Personas in the native host', async () => {
    const transport = new StubControlTransport('native', {
      'agent.sessions.list': { ok: true, items: [] },
      'agent.roles.list': { ok: true, items: [] },
      'agent.tools.list': { ok: true, items: [] },
    });
    render(
      <MemoryRouter initialEntries={['/agent']}>
        <ControlTransportProvider transport={transport}>
          <QueryClientProvider client={testQueryClient()}>
            <TooltipProvider><AgentFeature /></TooltipProvider>
          </QueryClientProvider>
        </ControlTransportProvider>
      </MemoryRouter>,
    );

    await waitFor(() => expect(transport.requests.map((request) => request.pathId)).toEqual(
      expect.arrayContaining(['agent.sessions.list', 'agent.roles.list']),
    ));
    expect(screen.getByText('0 段对话 · 0 个项目')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '还没有对话' })).toBeVisible();
    expect(within(screen.getByLabelText('对话与项目')).getByRole('button', { name: '新建对话' })).toBeEnabled();
    expect(screen.queryByText('控制中心迁移')).not.toBeInTheDocument();
    expect(screen.queryByText('记忆整理')).not.toBeInTheDocument();
  });

  it('distinguishes a Session load error from an authoritative empty result and can retry', async () => {
    let attempts = 0;
    const transport = new StubControlTransport('native', {
      'agent.sessions.list': () => {
        attempts += 1;
        if (attempts === 1) throw new Error('Session 服务暂不可用');
        return { ok: true, items: [] };
      },
      'agent.roles.list': { ok: true, items: [] },
      'agent.tools.list': { ok: true, items: [] },
    });
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={['/agent']}>
        <ControlTransportProvider transport={transport}>
          <QueryClientProvider client={testQueryClient()}>
            <TooltipProvider><AgentFeature /></TooltipProvider>
          </QueryClientProvider>
        </ControlTransportProvider>
      </MemoryRouter>,
    );

    const shellHeading = await screen.findByRole('heading', { name: '无法读取对话' });
    const rail = screen.getByLabelText('对话与项目');
    expect(shellHeading).toBeVisible();
    expect(shellHeading.closest('[role="alert"]')).toBeNull();
    expect(within(rail).getByRole('alert')).toHaveTextContent('Session 服务暂不可用');
    expect(within(rail).getByRole('button', { name: '重新读取' })).toBeEnabled();
    expect(screen.queryByRole('heading', { name: '还没有对话' })).not.toBeInTheDocument();

    await user.click(within(rail).getByRole('button', { name: '重新读取' }));

    expect(await screen.findByRole('heading', { name: '还没有对话' })).toBeVisible();
    expect(screen.queryByRole('heading', { name: '无法读取对话' })).not.toBeInTheDocument();
  });
});

function renderAgent(transport: ControlTransport, initialEntry = '/agent') {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <ControlTransportProvider transport={transport}>
        <QueryClientProvider client={testQueryClient()}>
          <TooltipProvider><AgentFeature /></TooltipProvider>
        </QueryClientProvider>
      </ControlTransportProvider>
    </MemoryRouter>,
  );
}

function setDocumentVisibility(state: DocumentVisibilityState): void {
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    value: state,
  });
}

function testQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function busyStopSnapshot(sessionId: string, sequence: number) {
  const turnId = `${sessionId}:turn-stop-reconcile`;
  const createdAtMs = Date.now() - 1_000;
  return {
    schemaVersion: 'rag-ime.agent-message-list.v1',
    ok: true,
    sessionId,
    status: 'busy',
    lastSequence: sequence,
    resumeToken: `${sessionId}:${sequence}`,
    liveEvents: [],
    items: [{
      schemaVersion: 'rag-ime.agent-message.v1',
      id: `${sessionId}:stop-user`,
      sessionId,
      turnId,
      role: 'user',
      status: 'completed',
      blocks: [{
        id: 'stop-user-text',
        type: 'text',
        status: 'completed',
        presentationKind: 'markdown',
        data: { text: '停止当前回合' },
      }],
      attachments: [],
      citations: [],
      createdAtMs,
      completedAtMs: createdAtMs,
    }, {
      schemaVersion: 'rag-ime.agent-message.v1',
      id: `${sessionId}:stop-assistant`,
      sessionId,
      turnId,
      role: 'assistant',
      status: 'streaming',
      blocks: [{
        id: 'stop-assistant-text',
        type: 'text',
        status: 'running',
        presentationKind: 'markdown',
        data: { text: 'partial' },
      }],
      attachments: [],
      citations: [],
      createdAtMs: createdAtMs + 100,
      completedAtMs: null,
    }],
  };
}

function abortedStopSnapshot(sessionId: string, sequence: number) {
  const snapshot = busyStopSnapshot(sessionId, sequence);
  return {
    ...snapshot,
    status: 'idle',
    items: snapshot.items.map((message) => (
      message.role === 'assistant'
        ? {
            ...message,
            status: 'aborted',
            blocks: message.blocks.map((block) => ({
              ...block,
              status: 'aborted',
              data: { text: '已停止。' },
            })),
            completedAtMs: Date.now(),
          }
        : message
    )),
  };
}

function featureTransport(
  modelCatalog: unknown = previewModelCatalog('session-preview'),
  toolRoute: unknown = undefined,
  sessionRoute: unknown = { ok: true, items: previewSessions },
  promptRoute: unknown = { ok: true },
  subagentRoute: unknown = { ok: true, items: [] },
  approvalRoute: unknown = { ok: true },
  abortRoute: unknown = { ok: true },
  runtimeRoute: unknown = {
    schemaVersion: 'rag-ime.agent-runtime.v1',
    enabled: true,
    managed: true,
    status: 'ready',
    driverId: 'managed-pi',
    runtimeKind: 'pi_rpc',
    runtimeVersion: 'preview',
    piVersion: '0.80.7',
    idleTimeoutSeconds: 600,
    activeSessionId: null,
    lastError: '',
    capabilities: { conversationFork: true, conversationRewrite: true },
  },
  snapshotRoute: unknown = previewAgentSnapshot('session-preview'),
  forkCreateRoute: unknown = {
    schemaVersion: 'rag-ime.agent-session-fork-create.v1',
    ok: true,
    sourceSessionId: 'session-preview',
    entryId: 'session-preview:user-media',
    selectedText: '读取输入法工具书，并把结果作为可展开卡片保留。',
    session: {
      ...previewSessions[0],
      schemaVersion: 'rag-ime.agent-session.v1',
      id: 'session-forked',
      title: '控制中心迁移 · 分支',
      createdAtMs: Date.now() - 1_000,
      messageCount: 2,
      updatedAtMs: Date.now(),
    },
  },
  modelSelectRoute: unknown = { ok: true },
  thinkingSelectRoute: unknown = { ok: true },
  modeUpdateRoute: unknown = (request: ControlRequest) => {
    const body = request.body && typeof request.body === 'object' && !Array.isArray(request.body)
      ? request.body as Record<string, unknown>
      : {};
    const workspaceRoots = Array.isArray(body.workspaceRoots)
      ? body.workspaceRoots.filter((value): value is string => typeof value === 'string')
      : previewSessions[0]!.workspaceRoots;
    const executionMode = typeof body.executionMode === 'string'
      ? body.executionMode as SessionSummary['executionMode']
      : previewSessions[0]!.executionMode;
    return {
      ok: true,
      session: {
        ...previewSessions[0]!,
        ...(body.mode === 'assistant' || body.mode === 'coordinator'
          ? { mode: body.mode }
          : {}),
        ...(typeof body.toolProfileVersion === 'string'
          ? { toolProfileVersion: body.toolProfileVersion }
          : {}),
        ...(executionMode ? { executionMode } : {}),
        projectContextEnabled: body.projectContextEnabled === true,
        piSkillsEnabled: body.piSkillsEnabled === true,
        codexSkillsEnabled: body.codexSkillsEnabled === true,
        workspaceRoots,
        workspaceScopeGranted: workspaceRoots.length > 0
          && (executionMode === 'workspace_managed' || executionMode === 'full_trust'),
      },
    };
  },
  forkListRoute: unknown = forkListFixture(),
  rewriteRoute: unknown = { ok: true },
): MockControlTransport {
  const normalizedToolRoute = toolRoute === undefined
    ? (request: ControlRequest) => capabilityToolCatalog(
      toolCatalog(),
      typeof request.query?.sessionId === 'string' ? request.query.sessionId : 'session-preview',
    )
    : normalizeCapabilityToolRoute(toolRoute);
  return new MockControlTransport({
    pickedFiles: [{
      id: 'media_fixture_attachment_01',
      name: 'screen.png',
      mimeType: 'image/png',
      byteSize: 68,
      sessionId: 'session-preview',
      sha256: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
    }],
    importedFiles: [{
      id: 'media_fixture_attachment_01',
      name: 'screen.png',
      mimeType: 'image/png',
      byteSize: 68,
      sessionId: 'session-preview',
      sha256: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
    }],
    routes: {
      'agent.sessions.list': sessionRoute,
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.tools.list': normalizedToolRoute,
      'agent.runtime.get': runtimeRoute,
      'agent.session.snapshot': snapshotRoute,
      'agent.session.workflow.get': workflowRouteFixture(),
      'agent.session.goal.mutate': workflowRouteFixture(),
      'agent.session.models': modelCatalog,
      'agent.session.commands': commandCatalog(),
      'agent.session.prompt': promptRoute,
      'agent.session.rewrite': rewriteRoute,
      'agent.session.forks.list': forkListRoute,
      'agent.session.forks.create': forkCreateRoute,
      'agent.sessions.create': { ok: true },
      'agent.session.rename': { ok: true },
      'agent.session.compact': { ok: true },
      'agent.session.abort': abortRoute,
      'agent.session.mode.update': modeUpdateRoute,
      'agent.session.model.select': modelSelectRoute,
      'agent.session.thinking.select': thinkingSelectRoute,
      'agent.approval.decide': approvalRoute,
      'agent.memoryMaintenance.run': memoryRunFixture(),
      'agent.session.review.resolve': { ok: true },
      'agent.subagents.list': subagentRoute,
      'agent.subagents.templates': {
        schemaVersion: 'rag-ime.agent-template-list.v1',
        ok: true,
        maxParallel: 2,
        maxDepth: 2,
        items: previewTemplates,
      },
      'agent.subagents.create': { ok: true, accepted: true, batch: { runs: [] } },
      'knowledge.database.draft.edit': { ok: true },
      'knowledge.database.apply.preview': {
        ok: true,
        previewToken: 'preview-token',
        payloadSha256: 'sha256:preview',
        expectedRevision: { runtimeRevision: 1 },
        summary: { items: ['应用已选择的记忆变更'] },
      },
      'knowledge.database.apply': { ok: true },
    },
  });
}

function forkListFixture() {
  return {
    schemaVersion: 'rag-ime.agent-session-fork-candidates.v1',
    ok: true,
    sessionId: 'session-preview',
    items: [
      { entryId: 'session-preview:user-architecture', text: '把迁移进度按真实代码链整理一下，别把工具日志当回答。', role: 'user', createdAtMs: 0 },
      { entryId: 'session-preview:assistant-architecture', text: '三条工作线已经收束到同一个 Todo。', role: 'assistant', createdAtMs: 0 },
      { entryId: 'session-preview:user-media', text: '读取输入法工具书，并把结果作为可展开卡片保留。', role: 'user', createdAtMs: 0 },
      { entryId: 'session-preview:assistant-media', text: '已完成。正文展示工具书内容，精确接口与参数继续留在右侧运行状态中。', role: 'assistant', createdAtMs: 0 },
      { entryId: 'internal-context', text: '<rag-ime-deep-search-context>private deep-search evidence</rag-ime-deep-search-context>', role: 'user', createdAtMs: 0 },
    ],
  };
}

function workflowRouteFixture() {
  return {
    schemaVersion: 'rag-ime.agent-workflow-state.v1',
    ok: true,
    sessionId: 'session-preview',
    todo: {
      schemaVersion: 'rag-ime.agent-todo.v1',
      id: 'todo:preview',
      sessionId: 'session-preview',
      revision: 4,
      actor: 'agent',
      updatedAtMs: Date.now(),
      phases: [
        {
          name: '实现',
          tasks: [
            { content: '核对现状', status: 'completed' },
            { content: '接入前端', status: 'in_progress' },
          ],
        },
        {
          name: '验证',
          tasks: [{ content: '运行验收', status: 'pending' }],
        },
      ],
      counts: { total: 3, pending: 1, inProgress: 1, completed: 1, abandoned: 0 },
    },
    goal: {
      schemaVersion: 'rag-ime.agent-goal.v1',
      sessionId: 'session-preview',
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
      todoRevision: 4,
      goalRevision: 0,
    },
  };
}

function subagentListFixture() {
  const now = Date.now();
  const run = (
    id: string,
    templateId: 'researcher' | 'planner' | 'worker' | 'reviewer',
    state: 'queued' | 'running' | 'completed' | 'failed',
    task: string,
    lineage: { batchId: string; parentRunId?: string; depth?: 1 | 2; ordinal?: number },
  ) => ({
    schemaVersion: 'rag-ime.agent-subagent-run.v1',
    id,
    nodeId: `node-${id}`,
    attemptId: `attempt-${id}-1`,
    attemptNumber: 1,
    predecessorAttemptId: '',
    ownerRunId: lineage.parentRunId || `batch:${lineage.batchId}`,
    parentRunId: lineage.parentRunId || '',
    depth: lineage.depth || 1,
    batchId: lineage.batchId,
    childSessionId: `session-child-${id}`,
    todoTask: '接入前端',
    todoPhase: '实现',
    templateId,
    templateVersion: '1',
    ordinal: lineage.ordinal || 0,
    task,
    expectedOutput: '公开进度与可验证交付',
    acceptanceCriteria: [],
    launchDigest: {
      schemaVersion: 'rag-ime.agent-subagent-launch-digest.v1',
      contextMode: lineage.parentRunId ? 'fork' : 'fresh',
      templateId,
      templateVersion: '1',
      modelProfile: 'test/model',
      thinkingLevel: 'medium',
      toolProfileVersion: templateId === 'worker' ? 'subagent-worker-v1' : 'subagent-readonly-v1',
      toolAllowlistMode: 'profile',
      tools: templateId === 'worker' ? ['workspace_read', 'workspace_shell'] : ['workspace_read'],
      piSkillsEnabled: true,
      codexSkillsEnabled: false,
      workspaceAccess: templateId === 'worker' ? 'write' : 'read_only',
      workspaceRootCount: 1,
      outputContract: { required: false, schemaSha256: '' },
      extensionRuntime: 'pi_host_managed',
    },
    contract: { status: 'not_requested', error: '', toolCallId: '', validatedAtMs: null },
    state,
    budget: { maxTurns: 10, maxToolCalls: 18, maxTotalTokens: 32_000, maxDurationMs: 300_000, maxOutputChars: 24_000 },
    usage: { turnCount: state === 'completed' ? 4 : 1, toolCount: state === 'completed' ? 3 : 0, totalTokens: state === 'completed' ? 2_400 : 320 },
    result: state === 'completed' ? { summary: '证据已经交回主对话。' } : {},
    error: state === 'failed' ? 'public failure' : '',
    resultContextScheduledAtMs: null,
    createdAtMs: now - 20_000,
    startedAtMs: state === 'queued' ? null : now - 14_000,
    updatedAtMs: now,
    completedAtMs: state === 'completed' || state === 'failed' ? now : null,
  });
  const research = run('run-research', 'researcher', 'running', '核对记忆设计与来源', {
    batchId: 'batch-status-roots',
  });
  const plan = run('run-plan', 'planner', 'queued', '整理实现顺序', {
    batchId: 'batch-status-pattern',
    parentRunId: research.id,
    depth: 2,
  });
  const review = run('run-review', 'reviewer', 'completed', '审阅公开结果', {
    batchId: 'batch-status-roots',
    ordinal: 1,
  });
  const work = run('run-work', 'worker', 'failed', '验证受控执行路径', {
    batchId: 'batch-status-worker',
  });
  const batch = (
    id: string,
    parentRunId: string,
    runs: ReturnType<typeof run>[],
  ) => ({
    schemaVersion: 'rag-ime.agent-subagent-batch.v1',
    id,
    parentSessionId: 'session-preview',
    parentRunId,
    contextMode: parentRunId ? 'fork' : 'fresh',
    resultDeliveryMode: 'inline',
    state: 'running',
    depth: parentRunId ? 1 : 0,
    maxDepth: 2,
    abortRequested: false,
    causalMetadata: {
      todoId: 'todo:frontend',
      todoRevision: 1,
      goalId: 'goal:agent-ui',
      goalRevision: 1,
      roomBound: false,
      roomId: '',
      rootId: '',
      taskId: '',
      dispatchId: '',
      generation: 0,
    },
    createdAtMs: now - 20_000,
    updatedAtMs: now,
    completedAtMs: null,
    runs,
  });
  return {
    ok: true,
    items: [
      batch('batch-status-roots', '', [research, review]),
      batch('batch-status-pattern', research.id, [plan]),
      batch('batch-status-worker', '', [work]),
    ],
    tree: {
      schemaVersion: 'rag-ime.agent-subagent-tree.v1',
      rootSessionId: 'session-preview',
      nodeCount: 4,
      maxDepth: 2,
      roots: [
        { run: research, children: [{ run: plan, children: [] }] },
        { run: review, children: [] },
        { run: work, children: [] },
      ],
    },
  };
}

function memoryRunFixture() {
  return {
    ok: true,
    run: {
      runId: 'memory-run-1',
      status: 'draft',
      summary: '输入法记忆增量整理',
      diffCount: 2,
      pendingDiffCount: 2,
      changes: [
        { diffId: 1, operationLabel: '更新主题书', status: 'pending', selected: true, title: '输入法 Agent 交互', detail: '记录审阅状态机决策', sourceCount: 3 },
        { diffId: 2, operationLabel: '归档重复内容', status: 'pending', selected: true, title: '旧的重复记录', detail: '仅保留来源引用', sourceCount: 2 },
      ],
    },
  };
}

function productionTransport(
  overrides: Partial<Record<string, unknown>> = {},
): StubControlTransport {
  return new StubControlTransport('native', {
    'agent.sessions.list': { ok: true, activeSessionId: 'session-preview', items: previewSessions },
    'agent.roles.list': { ok: true, items: previewPersonas },
    'agent.tools.list': (request: ControlRequest) => capabilityToolCatalog(
      toolCatalog(),
      typeof request.query?.sessionId === 'string' ? request.query.sessionId : 'session-preview',
    ),
    'agent.session.snapshot': previewAgentSnapshot('session-preview'),
    'agent.session.models': previewModelCatalog('session-preview'),
    'agent.session.commands': commandCatalog(),
    ...overrides,
  } as ConstructorParameters<typeof StubControlTransport>[1]);
}

function commandCatalog() {
  return {
    schemaVersion: 'rag-ime.agent-command-catalog.v1',
    ok: true,
    sessionId: 'session-preview',
    runtimeAvailable: true,
    items: [
      { name: 'review', invocation: '/review', description: '审阅当前改动', source: 'extension' },
      { name: 'subagents', invocation: '/subagents', description: '查看子 Agent', source: 'extension' },
      { name: 'plan', invocation: '/plan', description: '运行规划模板', source: 'prompt' },
      { name: 'skill:browser', invocation: '/skill:browser', description: '调用浏览器技能', source: 'skill' },
    ],
  };
}

function lunaModelCatalog(): ModelCatalog {
  return {
    schemaVersion: 'rag-ime.agent-model-catalog.v1',
    ok: true,
    sessionId: 'session-preview',
    selected: { provider: 'gpt', id: 'gpt-5.6-luna', modelId: 'gpt-5.6-luna', name: 'GPT-5.6 Luna' },
    thinkingLevel: 'medium',
    providers: [{
      id: 'gpt',
      displayName: 'GPT',
      models: [
        modelFixture('codex-mini-latest', 'Codex Mini'),
        modelFixture('gpt-5.6-luna', 'GPT-5.6 Luna', ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']),
      ],
    }],
  };
}

function modelFixture(id: string, name: string, thinkingLevels: ThinkingLevel[] = ['off', 'medium']) {
  return { provider: 'gpt', id, name, api: 'responses', reasoning: true, thinkingLevels: thinkingLevels as [ThinkingLevel, ...ThinkingLevel[]], supportsImages: true, contextWindow: 100_000, maxTokens: 32_000 };
}

function toolCatalog() {
  const tools = [
    ['overview', '控制中心概览', 'overview'],
    ['input', '输入法', 'input'],
    ['voice', '语音输入', 'voice'],
    ['planning', '规划与任务', 'planning'],
    ['memory', '记忆与工具书', 'memory'],
    ['knowledge', '知识检索', 'knowledge'],
    ['models', '模型', 'models'],
    ['runtime', '诊断与运行时', 'runtime'],
    ['configuration', '历史与配置', 'configuration'],
    ['agents', '多 Agent 协作', 'agents'],
    ['workspace_list', '工作区浏览', 'workspace'],
    ['workspace_read', '工作区读取', 'workspace'],
    ['workspace_shell', '受控命令', 'workspace'],
    ['workspace_job', '后台任务', 'workspace'],
  ] as const;
  return tools.map(([id, displayName, category]) => ({
    schemaVersion: 'rag-ime.control-tool-manifest.v1',
    id,
    domain: category,
    displayName,
    description: `${displayName}真实能力`,
    riskLevel: id === 'workspace_shell' || id === 'workspace_job' ? 'R2' : 'R0',
    operationRisks: { status: id === 'workspace_shell' || id === 'workspace_job' ? 'R2' : 'R0' },
    sessionModes: id.startsWith('workspace_') ? ['coordinator'] : ['assistant', 'coordinator'],
    operations: ['status'],
    resultPresentation: 'tool_result',
    availability: 'online',
    version: '1',
  }));
}

type ToolCatalogFixture = ReturnType<typeof toolCatalog>[number] & { enabled?: boolean };

function normalizeCapabilityToolRoute(value: unknown): unknown {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return value;
  const route = value as Record<string, unknown>;
  if (route.schemaVersion !== undefined || !Array.isArray(route.items)) return value;
  return (request: ControlRequest) => capabilityToolCatalog(
    route.items as ToolCatalogFixture[],
    typeof request.query?.sessionId === 'string' ? request.query.sessionId : 'session-preview',
  );
}

function capabilityToolCatalog(tools: readonly ToolCatalogFixture[] = toolCatalog(), sessionId = 'session-preview') {
  const effectiveAtMs = 1;
  const toolItems = tools.map((tool) => {
    const enabled = tool.enabled !== false;
    const canonicalId = `tool:${tool.id}`;
    return {
      ...tool,
      canonicalId,
      kind: 'tool',
      source: { kind: 'product', label: 'Personal Agent Workbench' },
      status: tool.availability,
      risk: tool.riskLevel,
      requiredPermissions: [],
      authorization: {
        state: enabled ? 'authorized' : 'denied',
        reason: enabled
          ? 'existing_session_policy_authorizes_tool'
          : 'existing_session_policy_does_not_authorize_tool',
      },
      disclosure: {
        preference: 'inherit',
        effective: 'enabled',
        state: 'disclosed',
        reason: 'inherited_built_in_default',
      },
      effectiveScope: 'built_in_default',
      reasons: [
        'inherited_built_in_default',
        enabled
          ? 'tool_authorized_by_existing_session_policy'
          : 'tool_not_authorized_by_existing_session_policy',
      ],
      revision: `tool-spec:${tool.version}`,
      effectiveAtMs,
    };
  });
  const items = [
    ...toolItems,
    {
      id: 'browser',
      canonicalId: 'skill:browser',
      kind: 'skill',
      displayName: 'browser',
      description: '按需加载浏览器 Skill',
      source: { kind: 'governed_native', label: 'Pi native Skills' },
      status: 'ready',
      risk: 'R0',
      requiredPermissions: ['skill_load'],
      authorization: {
        state: 'authorized',
        reason: 'existing_skill_policy_authorizes_load',
      },
      disclosure: {
        preference: 'inherit',
        effective: 'enabled',
        state: 'disclosed',
        reason: 'inherited_built_in_default',
      },
      effectiveScope: 'built_in_default',
      reasons: ['inherited_built_in_default', 'skill_authorized_by_existing_room_policy'],
      revision: 'skill:1',
      effectiveAtMs,
    },
  ];
  return {
    schemaVersion: 'rag-ime.capability-catalog.v1',
    ok: true,
    revision: `sha256:${'a'.repeat(64)}`,
    effectiveAtMs,
    projectScope: {
      supported: true,
      identityKind: 'workspace_scope_sha256',
      projectId: `workspace-${'b'.repeat(64)}`,
      reason: 'session_workspace_scope',
    },
    sessionPolicy: {
      sessionId,
      policyRevision: 1,
      disclosurePreferences: {
        globalDefault: {},
        projectDefault: {},
        session: {},
        effective: Object.fromEntries(items.map((item) => [item.canonicalId, 'enabled'])),
      },
      effectiveAtMs,
    },
    items,
  };
}

function historyMessage(
  sessionId: string,
  id: string,
  role: 'user' | 'assistant',
  text: string,
) {
  return {
    schemaVersion: 'rag-ime.agent-message.v1',
    id,
    sessionId,
    turnId: 'history',
    role,
    status: 'completed',
    blocks: [{
      id: `${id}:text`,
      type: 'text',
      status: 'completed',
      presentationKind: 'markdown',
      data: { text },
    }],
    attachments: [],
    citations: [],
    createdAtMs: 100,
    completedAtMs: 101,
  };
}
