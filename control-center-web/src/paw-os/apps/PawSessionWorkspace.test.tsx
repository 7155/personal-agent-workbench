import { forwardRef, type Key, type ReactNode } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ControlTransportProvider } from '@/app/control-transport';
import { createPreviewTransport } from '@/app/preview-control-transport';
import { TooltipProvider } from '@/components/primitives';
import { createAgentProjection, type AgentProjectionState } from '@/contracts/agent-reducer';
import { parseAgentEvent } from '@/contracts/validators';
import { SessionSubagentPanel } from '@/features/agent/delegation/SessionSubagentPanel';
import { useAgentLiveStore } from '@/features/agent/state/live-store';
import type { SessionSummary } from '@/features/agent/types';
import { TeamProvider } from '@/features/team/team-context';
import type { TeamApi } from '@/features/team/team-api';
import { PawOsDesktopProvider } from '@/features/paw-os/surface-context';
import { StubControlTransport } from '@/test/stub-control-transport';
import { ControlTransportHttpError } from '@/platform/http-transport';
import type { ControlEventObserver, ControlRequest } from '@/platform/transport';
import { parseTraceAgentHandoff } from '@/features/trace-agent/handoff';
import agentMigratedCss from '../styles/paw-os-agent-migrated-v1.css?raw';
import appsCss from './paw-apps.css?raw';
import { PawWindowFrame } from '../shell/PawWindowLayer';
import { PawSessionWorkspace, sessionWorkspaceProjectionSlice } from './PawSessionWorkspace';

/* jsdom gives every row zero height, so the real virtualizer would keep the
   transcript empty and no timeline assertion here would mean anything. */
vi.mock('react-virtuoso', () => ({
  Virtuoso: forwardRef(function MockVirtuoso({
    components,
    computeItemKey,
    context,
    data,
    itemContent,
    scrollerRef,
  }: {
    components?: { Header?: (props: { context?: unknown }) => ReactNode; Footer?: () => ReactNode };
    computeItemKey?: (index: number, item: string) => Key;
    context?: unknown;
    data: string[];
    itemContent: (index: number, item: string) => ReactNode;
    scrollerRef?: (scroller: HTMLElement | Window | null) => void;
  }) {
    const Header = components?.Header;
    const Footer = components?.Footer;
    return (
      <div data-testid="virtuoso-list" ref={(node) => scrollerRef?.(node)}>
        {Header ? <Header context={context} /> : null}
        {data.map((item, index) => (
          <div key={computeItemKey?.(index, item) ?? index}>{itemContent(index, item)}</div>
        ))}
        {Footer ? <Footer /> : null}
      </div>
    );
  }),
}));

afterEach(() => {
  delete window.pawBrowserHost;
  cleanup();
  document.querySelector('meta[name="paw-deployment"]')?.remove();
});

describe('PAWOS Agent Session structural migration', () => {
  it.each(['missing', 'provisional'])('does not invent a permission preset with %s canonical Session metadata', async (kind) => {
    const transport = new StubControlTransport('mock', idleSessionRoutes());
    const workspace = (record?: SessionSummary, known = Boolean(record)) => <ControlTransportProvider transport={transport}><TooltipProvider>
      <PawSessionWorkspace record={record} recordMetadataKnown={known} recordId="session-live"
        onNewWork={vi.fn()} onSessionCreated={vi.fn()} onSessionUpdated={vi.fn()} />
    </TooltipProvider></ControlTransportProvider>;
    const view = render(workspace(kind === 'provisional' ? { ...liveSession(), mode: 'assistant', executionMode: undefined, toolProfileVersion: undefined } : undefined, false));
    expect(screen.getByRole('button', { name: '对话权限：尚未同步' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: '对话权限：写入与命令确认' })).not.toBeInTheDocument();
    const user = userEvent.setup();
    await user.type(screen.getByRole('textbox', { name: '消息' }), '保留正在输入的内容');
    view.rerender(workspace({ ...liveSession(), executionMode: 'full_trust', toolProfileVersion: 'control-center-auto-approve-v1' }));
    expect(await screen.findByRole('button', { name: '对话权限：全自动' })).toBeEnabled();
    expect(screen.getByRole('textbox', { name: '消息' })).toHaveValue('保留正在输入的内容');
  });

  it('opens the linked memory control while model discovery is still pending', async () => {
    const transport = createPreviewTransport();
    const request = transport.request.bind(transport);
    const delayedModel = deferred<unknown>();
    vi.spyOn(transport, 'request').mockImplementation((input) => input.pathId === 'agent.session.models' ? delayedModel.promise as never : request(input));
    render(<ControlTransportProvider transport={transport}><TooltipProvider>
      <PawSessionWorkspace record={liveSession()} recordId="session-live"
        toolPickerIntent={{ id: 'memory-request-1', query: '记忆' }}
        onNewWork={vi.fn()} onSessionCreated={vi.fn()} onSessionUpdated={vi.fn()} />
    </TooltipProvider></ControlTransportProvider>);
    const search = await screen.findByRole('textbox', { name: '搜索工具' });
    expect(search).toHaveValue('记忆');
    expect(screen.getByRole('combobox', { name: '记忆召回的当前对话使用' })).toBeVisible();
    await act(async () => { delayedModel.resolve({}); });
  });

  it('keeps transient connection recovery out of the operation-failure alert and preserves the draft', async () => {
    const sessionId = 'session-quiet-reconnect';
    const transport = new StubControlTransport('mock', idleSessionRoutes());
    const observers: ControlEventObserver<unknown>[] = [];
    vi.spyOn(transport, 'subscribe').mockImplementation((_request, observer) => { observers.push(observer); return () => undefined; });
    render(<ControlTransportProvider transport={transport}><TooltipProvider>
      <PawSessionWorkspace record={{ ...liveSession(), id: sessionId }} recordId={sessionId}
        onNewWork={vi.fn()} onSessionCreated={vi.fn()} onSessionUpdated={vi.fn()} />
    </TooltipProvider></ControlTransportProvider>);
    await waitFor(() => expect(observers).toHaveLength(1));
    const composer = screen.getByRole('textbox', { name: '消息' });
    await userEvent.setup().type(composer, '保留这段草稿');
    act(() => observers[0]!.error?.(new Error('temporary disconnect')));
    expect(screen.queryByText('Session 操作没有完成，请重新同步后重试。')).not.toBeInTheDocument();
    expect(screen.getByText('正在恢复连接')).toBeVisible();
    expect(composer).toHaveValue('保留这段草稿');
    expect(transport.requests.filter((request) => request.pathId === 'agent.session.prompt')).toHaveLength(0);
  });

  it('clears connection recovery when the stream stabilizes even if the repair snapshot failed', async () => {
    const sessionId = 'session-stream-restores-without-snapshot';
    let failSnapshot = false;
    const transport = new StubControlTransport('mock', { ...idleSessionRoutes(),
      'agent.session.snapshot': () => { if (failSnapshot) throw new Error('snapshot temporarily unavailable'); return idleSessionRoutes()['agent.session.snapshot']; },
    });
    const observers: ControlEventObserver<unknown>[] = [];
    vi.spyOn(transport, 'subscribe').mockImplementation((_request, observer) => { observers.push(observer); return () => undefined; });
    render(<ControlTransportProvider transport={transport}><TooltipProvider>
      <PawSessionWorkspace record={{ ...liveSession(), id: sessionId }} recordId={sessionId}
        onNewWork={vi.fn()} onSessionCreated={vi.fn()} onSessionUpdated={vi.fn()} />
    </TooltipProvider></ControlTransportProvider>);
    await waitFor(() => expect(observers).toHaveLength(1));
    failSnapshot = true;
    act(() => observers[0]!.error?.(new Error('temporary disconnect')));
    await waitFor(() => expect(observers.length).toBeGreaterThan(1), { timeout: 6000 });
    act(() => observers.at(-1)!.stable?.(''));
    expect(screen.queryByText('Session 操作没有完成，请重新同步后重试。')).not.toBeInTheDocument();
    expect(screen.queryByText('正在恢复连接')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '立即重连' })).not.toBeInTheDocument();
    expect(transport.requests.filter((request) => request.pathId === 'agent.session.prompt')).toHaveLength(0);
  });

  it('sends a screen attachment with bounded source context through the existing prompt and restores it on failure', async () => {
    const sessionId = 'session-screen-prompt';
    const transport = idleSessionTransport();
    const context = { mediaId: 'media_abcdefghijklmnop', sourceAppBundleId: 'com.example.Editor', capturedAtMs: 1000 };
    useAgentLiveStore.getState().clear(sessionId);
    render(<ControlTransportProvider transport={transport}><TooltipProvider>
      <PawSessionWorkspace record={{ ...liveSession(), id: sessionId }} recordId={sessionId}
        initialDraft="翻译选区" initialAttachments={[{ id: context.mediaId, name: '选区.png', mimeType: 'image/png', byteSize: 64, source: 'picker' }]}
        appearance="embedded" showComposerControls screenContext={context}
        onNewWork={vi.fn()} onSessionCreated={vi.fn()} onSessionUpdated={vi.fn()} />
    </TooltipProvider></ControlTransportProvider>);
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await userEvent.setup().click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(transport.requests.find((request) => request.pathId === 'agent.session.prompt')?.body)
      .toMatchObject({ message: '翻译选区', attachments: [context.mediaId], screenContext: context }));
    await waitFor(() => expect(composer).toHaveValue('翻译选区'));
    expect(screen.getByText('选区.png')).toBeVisible();
    expect(screen.getByRole('button', { name: /对话权限/ })).toBeVisible();
    expect(screen.getByRole('button', { name: /模型与思考|模型.*推理|模型.*思考/ })).toBeVisible();
    useAgentLiveStore.getState().clear(sessionId);
  });

  it('lets the newest terminal turn end composer busy state even when an older turn is stale-running', () => {
    const sessionId = 'session-terminal-fence';
    const projection = createAgentProjection(sessionId);
    projection.turnOrder = ['turn-stale-running', 'turn-latest-completed'];
    projection.turnsById = {
      'turn-stale-running': {
        id: 'turn-stale-running', status: 'running', messageIds: ['message-old'], activityIds: [], createdAtMs: 1, updatedAtMs: 2,
      },
      'turn-latest-completed': {
        id: 'turn-latest-completed', status: 'completed', messageIds: ['message-final'], activityIds: [], createdAtMs: 3, updatedAtMs: 4,
      },
    };
    const state = {
      ...useAgentLiveStore.getState(),
      projections: { [sessionId]: projection },
    };

    expect(sessionWorkspaceProjectionSlice(state, sessionId).activeTurnId).toBe('');
  });
  it('keeps the Stop action available behind a rejected follow-up during provider retry', () => {
    const sessionId = 'retry-with-rejected-followup';
    const projection = createAgentProjection(sessionId);
    projection.status = 'retrying';
    projection.turnOrder = ['running', 'rejected'];
    projection.turnsById = {
      running: { id: 'running', status: 'running', messageIds: ['original'], activityIds: ['retry'], createdAtMs: 1, updatedAtMs: 3 },
      rejected: { id: 'rejected', status: 'failed', messageIds: ['followup'], activityIds: [], createdAtMs: 2, updatedAtMs: 2 },
    };
    projection.activityOrder = ['retry'];
    projection.activitiesById.retry = { id: 'retry', turnId: 'running', kind: 'status_changed', status: 'running',
      summary: '正在重试', payload: { phase: 'provider_retry' }, createdAtMs: 1, updatedAtMs: 3 };
    const state = { ...useAgentLiveStore.getState(), projections: { [sessionId]: projection } };
    expect(sessionWorkspaceProjectionSlice(state, sessionId).activeTurnId).toBe('running');
    projection.status = 'faulted';
    expect(sessionWorkspaceProjectionSlice(state, sessionId).activeTurnId).toBe('');
  });


  it('keeps Memory steward tool receipts visible but collapsed in the embedded conversation', async () => {
    const sessionId = 'session-memory-steward-tools';
    const transport = new StubControlTransport('mock', {
      'agent.session.snapshot': {
        messages: [],
        liveEvents: [parseAgentEvent({
          schemaVersion: 'rag-ime.agent-event.v1',
          eventId: `${sessionId}:1`,
          sessionId,
          turnId: 'turn-memory-search',
          sequence: 1,
          createdAtMs: 10,
          eventType: 'tool_finished',
          payload: {
            toolCallId: 'call-memory-search',
            toolName: 'memory',
            operation: 'search',
            summary: '已检索可验证的记忆来源',
            result: { summary: '命中 1 条记忆' },
          },
          resumeToken: `${sessionId}:1`,
        })],
        lastSequence: 1,
        resumeToken: `${sessionId}:1`,
        status: 'active',
      },
      'agent.session.models': {},
      'agent.session.commands': {},
      'agent.tools.list': {},
      'agent.runtime.get': {},
    });
    useAgentLiveStore.getState().clear(sessionId);
    const { container } = render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            appearance="embedded"
            record={{ ...liveSession(), id: sessionId }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    const receipt = await waitFor(() => {
      const node = container.querySelector<HTMLDetailsElement>('details.agent-activity--inline');
      expect(node).not.toBeNull();
      return node!;
    });
    expect(receipt).not.toHaveAttribute('open');
    expect(receipt).toHaveTextContent('命中 1 条记忆');
    useAgentLiveStore.getState().clear(sessionId);
  });

  it('keeps an inactive but visible Session window live without restarting its stream', async () => {
    const sessionId = 'session-inactive-gate';
    const transport = new StubControlTransport('mock', idleSessionRoutes());
    const props = {
      record: { ...liveSession(), id: sessionId },
      recordId: sessionId,
      onNewWork: vi.fn(),
      onSessionCreated: vi.fn(),
      onSessionUpdated: vi.fn(),
    };
    const { rerender } = render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace active={false} {...props} />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    await waitFor(() => expect(transport.subscriptionCount('agent.session.events')).toBe(1));
    expect(transport.requests.filter((request) => request.pathId === 'agent.session.snapshot')).toHaveLength(1);

    rerender(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace active {...props} />
        </TooltipProvider>
      </ControlTransportProvider>,
    );
    expect(transport.subscriptionCount('agent.session.events')).toBe(1);

    rerender(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace active={false} {...props} />
        </TooltipProvider>
      </ControlTransportProvider>,
    );
    expect(transport.subscriptionCount('agent.session.events')).toBe(1);
    expect(transport.requests.filter((request) => request.pathId === 'agent.session.snapshot')).toHaveLength(1);
  });

  it('opens an evaluation snapshot as a full read-only transcript without live Runtime controls', async () => {
    const sessionId = 'agent:evaluation-snapshot';
    const transport = new StubControlTransport('mock', idleSessionRoutes());

    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{ ...liveSession(), id: sessionId, evaluationSnapshot: true, executionMode: 'read_only' }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    expect(await screen.findByText('真实评测记录，只读')).toBeInTheDocument();
    await waitFor(() => expect(transport.requests.map((request) => request.pathId)).toEqual([
      'agent.session.snapshot',
    ]));
    expect(transport.subscriptionCount('agent.session.events')).toBe(0);
    expect(screen.queryByRole('textbox', { name: '消息' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Agent 轨迹' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Session 工具' })).toBeNull();
    expect(screen.getByText('评测快照')).toBeInTheDocument();
  });

  it('keeps another project member\'s Session read-only', async () => {
    const sessionId = 'team-owned-by-other';
    const transport = new StubControlTransport('mock', idleSessionRoutes());
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{ ...liveSession(), id: sessionId, canControl: false, ownerUserId: 'user-other', audience: 'project' }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    expect(await screen.findByText('仅可查看')).toBeVisible();
    expect(screen.queryByRole('textbox', { name: '消息' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '发布当前 Session 固定版本' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Agent 轨迹' })).toBeVisible();
  });

  it('lets the owner publish a Room partner checkout as a fixed project version', async () => {
    const meta = document.createElement('meta');
    meta.name = 'paw-deployment'; meta.content = 'team'; document.head.append(meta);
    const api = {
      status: vi.fn().mockResolvedValue({ enabled: true, name: 'Team' }),
      me: vi.fn().mockResolvedValue({
        user: { id: 'wang', username: 'wang', displayName: '小王', role: 'member', active: true },
        csrfToken: 'fixture', spaces: [{ id: 'website', kind: 'project', name: '官网', role: 'contributor', revision: 1 }],
      }),
    } as unknown as TeamApi;
    const record: SessionSummary = { ...liveSession(), id: 'owned-partner', canControl: true,
      audience: 'project', ownerUserId: 'wang',
      roomParticipant: { roomId: 'room-1', participantId: 'participant-wang', status: 'active' } };
    render(<TeamProvider api={api}>
      <ControlTransportProvider transport={new StubControlTransport('mock', idleSessionRoutes())}>
        <TooltipProvider><PawSessionWorkspace record={record} recordId={record.id}
          onNewWork={vi.fn()} onSessionCreated={vi.fn()} onSessionUpdated={vi.fn()} /></TooltipProvider>
      </ControlTransportProvider>
    </TeamProvider>);
    await userEvent.setup().click(await screen.findByRole('button', { name: '发布当前 Session 固定版本' }));
    expect(await screen.findByRole('heading', { name: '发布 Session 固定版本' })).toBeVisible();
  });

  it('gives the recent snapshot priority over nonessential control catalogs', async () => {
    let resolveSnapshot!: (value: unknown) => void;
    const snapshot = new Promise<unknown>((resolve) => { resolveSnapshot = resolve; });
    const transport = new StubControlTransport('mock', {
      'agent.session.snapshot': () => snapshot,
      'agent.session.models': {},
      'agent.session.commands': {},
      'agent.tools.list': {},
      'agent.runtime.get': {},
    });

    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={liveSession()}
            recordId="session-priority"
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );
    await act(async () => { await Promise.resolve(); });
    expect(transport.requests.map((request) => request.pathId)).toEqual([
      'agent.session.snapshot',
    ]);

    resolveSnapshot({
      messages: [], liveEvents: [], lastSequence: 0, resumeToken: '',
      status: 'idle', snapshotScope: 'recent', partial: true,
    });
    await waitFor(() => expect(transport.requests.map((request) => request.pathId)).toEqual([
      'agent.session.snapshot',
      'agent.session.models',
      'agent.session.commands',
      'agent.tools.list',
      'agent.runtime.get',
    ]));
  });

  it('does not initialize a visible Session while the document is hidden', async () => {
    const sessionId = 'session-hidden-gate';
    const transport = new StubControlTransport('mock', idleSessionRoutes());
    setDocumentVisibility('hidden');
    try {
      const { rerender } = render(
        <ControlTransportProvider transport={transport}>
          <TooltipProvider>
            <PawSessionWorkspace
              record={{ ...liveSession(), id: sessionId }}
              recordId={sessionId}
              onNewWork={vi.fn()}
              onSessionCreated={vi.fn()}
              onSessionUpdated={vi.fn()}
            />
          </TooltipProvider>
        </ControlTransportProvider>,
      );

      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(transport.requests).toHaveLength(0);
      expect(transport.subscriptionCount('agent.session.events')).toBe(0);

      setDocumentVisibility('visible');
      rerender(
        <ControlTransportProvider transport={transport}>
          <TooltipProvider>
            <PawSessionWorkspace
              record={{ ...liveSession(), id: sessionId }}
              recordId={sessionId}
              onNewWork={vi.fn()}
              onSessionCreated={vi.fn()}
              onSessionUpdated={vi.fn()}
            />
          </TooltipProvider>
        </ControlTransportProvider>,
      );
      await waitFor(() => expect(transport.subscriptionCount('agent.session.events')).toBe(1));
    } finally {
      setDocumentVisibility('visible');
    }
  });

  it.each([375, 360])('projects one complete Session chrome into a %ipx production window', async (width) => {
    render(
      <ControlTransportProvider transport={createPreviewTransport()}>
        <TooltipProvider>
          <PawWindowFrame
            active
            appId="agent"
            bounds={{ x: 0, y: 0, width, height: 720 }}
            onBoundsCommit={() => undefined}
            onClose={() => undefined}
            onFocus={() => undefined}
            onMinimize={() => undefined}
            onToggleMaximize={() => undefined}
            title="完整迁移"
            windowChrome="agent-session"
            windowId={`agent-${width}`}
            zIndex={10}
          >
            <PawSessionWorkspace
              record={liveSession()}
              recordId="session-live"
              onNewWork={vi.fn()}
              onSessionCreated={vi.fn()}
              onSessionUpdated={vi.fn()}
            />
          </PawWindowFrame>
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    await screen.findByRole('textbox', { name: '消息' });
    const window = screen.getByLabelText('完整迁移窗口');
    const titlebar = window.querySelector('.paw-window-titlebar') as HTMLElement;
    expect(titlebar.querySelectorAll('.paw-session-workspace__header')).toHaveLength(1);
    expect(within(titlebar).getByText('完整迁移')).toBeInTheDocument();
    expect(titlebar.querySelector('.paw-session-workspace__identity')).not.toBeInTheDocument();
    expect(window.querySelector('.paw-window-body .paw-session-workspace__header')).toBeNull();
    expect(within(titlebar).getByRole('button', { name: '对话' })).toBeInTheDocument();
    expect(within(titlebar).getByRole('button', { name: 'Agent 轨迹' })).toBeInTheDocument();
    expect(within(titlebar).getByRole('button', { name: '星空' })).toBeInTheDocument();
    expect(within(titlebar).getByRole('button', { name: 'Session 工具' })).toBeInTheDocument();
    const sessionHeader = titlebar.querySelector('.paw-session-workspace__header') as HTMLElement;
    expect(within(sessionHeader).getAllByRole('button')).toHaveLength(4);
    expect(within(titlebar).queryByRole('button', { name: '打开 Session 文件' })).not.toBeInTheDocument();
    expect(within(titlebar).queryByRole('button', { name: '打开子 Agent 工作台' })).not.toBeInTheDocument();
    expect(within(titlebar).queryByRole('button', { name: '打开 Session 任务中心' })).not.toBeInTheDocument();
    expect(window.querySelector('.paw-session-workspace__side')).toBeNull();
    const conversationNav = window.querySelector('.agent-conversation-nav');
    expect(conversationNav).not.toBeNull();
    expect(conversationNav?.querySelectorAll('button')).toHaveLength(2);
  });

  it('does not repeat workspace and permission context as a conversation header strip', async () => {
    const { container } = render(
      <ControlTransportProvider transport={createPreviewTransport()}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={liveSession()}
            recordId="session-live"
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    expect(screen.getByRole('textbox', { name: '消息' })).toBeVisible();
    expect(screen.queryByRole('note', { name: 'Session 上下文' })).not.toBeInTheDocument();
    expect(screen.queryByText('personal-agent-workbench · 工作区')).not.toBeInTheDocument();
    expect(screen.queryByText('权限 · 按风险确认')).not.toBeInTheDocument();
    // fx keeps message side as identity: no repeated "Agent/状态" caption row.
    expect(container.querySelector('.agent-assistant-turn__body > header')).toBeNull();
  });

  it('keeps one real composer mounted while switching between conversation and trace', async () => {
    const transport = createPreviewTransport();
    const user = userEvent.setup();
    const { container } = render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={liveSession()}
            recordId="session-live"
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    const composer = await screen.findByRole('textbox', { name: '消息' });
    expect(composer).toBeVisible();
    const conversation = container.querySelector('.paw-session-workspace__conversation');
    const trace = container.querySelector('.paw-session-workspace__trace');
    expect(conversation).not.toHaveAttribute('inert');
    expect(trace).toHaveAttribute('inert');
    expect(screen.queryByRole('complementary', { name: 'Session 工具侧栏' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Agent 轨迹' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Agent 轨迹' })).toHaveAttribute('aria-pressed', 'true'));
    expect(conversation).toHaveAttribute('inert');
    expect(trace).not.toHaveAttribute('inert');
    expect(screen.getByRole('textbox', { name: '消息' })).toBe(composer);
  });

  it('keeps the composer usable while a generic user input request is waiting', async () => {
    const sessionId = 'session-waiting-for-input';
    const transport = new StubControlTransport('mock', {
      'agent.session.snapshot': {
        messages: [],
        liveEvents: [parseAgentEvent({
          schemaVersion: 'rag-ime.agent-event.v1',
          eventId: `${sessionId}:1`,
          sessionId,
          turnId: 'turn-waiting-for-input',
          sequence: 1,
          createdAtMs: 10,
          eventType: 'user_input_required',
          payload: {
            requestId: 'request-waiting-for-input',
            requestKind: 'user_input_required',
            method: 'input',
            title: '补充实现范围',
            message: '你也可以继续给当前 Session 发普通消息。',
          },
          resumeToken: `${sessionId}:1`,
        })],
        lastSequence: 1,
        resumeToken: `${sessionId}:1`,
        status: 'busy',
      },
      'agent.session.models': {},
      'agent.session.commands': {},
      'agent.tools.list': {},
      'agent.runtime.get': {},
    });
    useAgentLiveStore.getState().clear(sessionId);
    const user = userEvent.setup();
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{ ...liveSession(), id: sessionId }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    expect(await screen.findByRole('region', { name: '补充实现范围' })).toBeVisible();
    const composer = screen.getByRole('textbox', { name: '消息' });
    expect(composer).toBeVisible();
    expect(composer).toBeEnabled();
    await user.type(composer, '先继续检查另一个文件');
    expect(composer).toHaveValue('先继续检查另一个文件');
    useAgentLiveStore.getState().clear(sessionId);
  });

  it('opens the 星空 view with the Session planet and its real subagent moons', async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { container } = render(
      <QueryClientProvider client={queryClient}>
        <ControlTransportProvider transport={createPreviewTransport()}>
          <TooltipProvider>
            <PawSessionWorkspace
              record={liveSession()}
              recordId="session-live"
              onNewWork={vi.fn()}
              onSessionCreated={vi.fn()}
              onSessionUpdated={vi.fn()}
            />
          </TooltipProvider>
        </ControlTransportProvider>
      </QueryClientProvider>,
    );

    await screen.findByRole('textbox', { name: '消息' });
    // Not watched → not mounted: the sky never polls behind the conversation.
    expect(screen.queryByRole('dialog', { name: 'Session 星空' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '星空' }));
    // This is the cold lazy-import acceptance path.  PawStarfield intentionally
    // stays out of the default Session bundle, so a clean test worker can spend
    // more than Testing Library's one-second default transforming the chunk.
    const sky = await screen.findByRole(
      'dialog',
      { name: 'Session 星空' },
      { timeout: 15_000 },
    );
    await within(sky).findByRole('button', { name: /研究员 卫星/ });
    expect(within(sky).getByRole('button', { name: /审阅者 卫星/ })).toBeInTheDocument();
    expect(container.querySelector('.paw-session-workspace__conversation')).toHaveAttribute('inert');
    expect(container.querySelector('.paw-session-workspace__starfield')).not.toHaveAttribute('inert');

    await user.click(within(sky).getByRole('button', { name: /返回对话/ }));
    expect(screen.queryByRole('dialog', { name: 'Session 星空' })).not.toBeInTheDocument();
    expect(container.querySelector('.paw-session-workspace__conversation')).not.toHaveAttribute('inert');

    await user.click(screen.getByRole('button', { name: '星空' }));
    const reopenedSky = await screen.findByRole('dialog', { name: 'Session 星空' });
    await user.click(within(reopenedSky).getByRole('button', { name: '子 Agent 工作台' }));
    expect(screen.queryByRole('dialog', { name: 'Session 星空' })).not.toBeInTheDocument();
    expect(screen.getByRole('complementary', { name: 'Session 子 Agent 工作台' })).toBeInTheDocument();
    expect(container.querySelector('.paw-session-workspace__conversation')).not.toHaveAttribute('inert');
  });

  it('opens every secondary tool from one menu into one mutually exclusive sidebar', async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <ControlTransportProvider transport={createPreviewTransport()}>
          <TooltipProvider>
            <PawSessionWorkspace
              record={liveSession()}
              recordId="session-live"
              onNewWork={vi.fn()}
              onSessionCreated={vi.fn()}
              onSessionUpdated={vi.fn()}
            />
          </TooltipProvider>
        </ControlTransportProvider>
      </QueryClientProvider>,
    );

    await screen.findByRole('textbox', { name: '消息' });
    expect(screen.queryByRole('complementary', { name: 'Session 工具侧栏' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Session 工具' }));
    const menu = screen.getByRole('menu', { name: 'Session 工具菜单' });
    expect(within(menu).getAllByRole('menuitem')).toHaveLength(3);
    await user.click(within(menu).getByRole('menuitem', { name: '文件' }));

    let sidebar = screen.getByRole('complementary', { name: 'Session 工具侧栏' });
    expect(sidebar.querySelectorAll(':scope > .agent-files-panel, :scope > .session-subagent-panel, :scope > .agent-status-panel')).toHaveLength(1);
    expect(sidebar.querySelector('.agent-files-panel')).not.toBeNull();

    await user.click(screen.getByRole('button', { name: 'Session 工具' }));
    await user.click(screen.getByRole('menuitem', { name: '任务与状态' }));
    sidebar = screen.getByRole('complementary', { name: 'Session 工具侧栏' });
    expect(sidebar.querySelectorAll(':scope > .agent-files-panel, :scope > .session-subagent-panel, :scope > .agent-status-panel')).toHaveLength(1);
    expect(sidebar.querySelector('.agent-files-panel')).toBeNull();
    expect(sidebar.querySelector('.agent-status-panel')).not.toBeNull();
    const criticalSteps = await within(sidebar).findByRole('button', { name: /关键步骤/ });
    const messageQueue = within(sidebar).getByRole('button', { name: /消息队列/ });
    expect(criticalSteps).toHaveAttribute('aria-expanded', 'false');
    expect(messageQueue).toHaveAttribute('aria-expanded', 'false');

    const statusPanel = sidebar.querySelector('.agent-status-panel');
    await user.click(within(sidebar).getByRole('button', { name: '收起任务中心' }));
    expect(screen.queryByRole('complementary', { name: 'Session 工具侧栏' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Session 工具' })).toHaveFocus();
    const residentSidebar = document.querySelector('.paw-session-workspace__side');
    expect(residentSidebar).toHaveAttribute('hidden');
    expect(residentSidebar?.querySelector('.agent-status-panel')).toBe(statusPanel);

    await user.click(screen.getByRole('button', { name: 'Session 工具' }));
    await user.click(screen.getByRole('menuitem', { name: '任务与状态' }));
    expect(document.querySelector('.paw-session-workspace__side .agent-status-panel')).toBe(statusPanel);
  });

  it('uses roving keyboard focus for the Session tools menu and restores the trigger on Escape', async () => {
    const user = userEvent.setup();
    render(
      <ControlTransportProvider transport={createPreviewTransport()}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={liveSession()}
            recordId="session-live"
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    await screen.findByRole('textbox', { name: '消息' });
    const trigger = screen.getByRole('button', { name: 'Session 工具' });
    await user.click(trigger);
    const menu = screen.getByRole('menu', { name: 'Session 工具菜单' });
    const items = within(menu).getAllByRole('menuitem');

    expect(items[0]).toHaveFocus();
    await user.keyboard('{ArrowDown}');
    expect(items[1]).toHaveFocus();
    await user.keyboard('{ArrowDown}');
    expect(items[2]).toHaveFocus();
    await user.keyboard('{ArrowDown}');
    expect(items[0]).toHaveFocus();
    await user.keyboard('{ArrowUp}');
    expect(items[2]).toHaveFocus();
    await user.keyboard('{Home}');
    expect(items[0]).toHaveFocus();
    await user.keyboard('{End}');
    expect(items[2]).toHaveFocus();

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('menu', { name: 'Session 工具菜单' })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it('closes on outside interaction and lets Tab leave the menu without a focus trap', async () => {
    const user = userEvent.setup();
    render(
      <ControlTransportProvider transport={createPreviewTransport()}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={liveSession()}
            recordId="session-live"
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    const composer = await screen.findByRole('textbox', { name: '消息' });
    const trigger = screen.getByRole('button', { name: 'Session 工具' });
    await user.click(trigger);
    expect(screen.getByRole('menu', { name: 'Session 工具菜单' })).toBeInTheDocument();
    await user.click(composer);
    expect(screen.queryByRole('menu', { name: 'Session 工具菜单' })).not.toBeInTheDocument();

    await user.click(trigger);
    const menu = screen.getByRole('menu', { name: 'Session 工具菜单' });
    const items = within(menu).getAllByRole('menuitem');
    await user.keyboard('{End}');
    expect(items[2]).toHaveFocus();
    await user.tab();
    expect(screen.queryByRole('menu', { name: 'Session 工具菜单' })).not.toBeInTheDocument();
  });

  it('closes the floating tool rail with Escape and returns focus to its trigger', async () => {
    const user = userEvent.setup();
    render(
      <ControlTransportProvider transport={createPreviewTransport()}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={liveSession()}
            recordId="session-live"
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    await screen.findByRole('textbox', { name: '消息' });
    await user.click(screen.getByRole('button', { name: 'Session 工具' }));
    await user.click(screen.getByRole('menuitem', { name: '文件' }));

    const sidebar = screen.getByRole('complementary', { name: 'Session 工具侧栏' });
    fireEvent.keyDown(sidebar, { key: 'Escape' });

    expect(screen.queryByRole('complementary', { name: 'Session 工具侧栏' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Session 工具' })).toHaveFocus();
  });

  it('returns focus to the Session tools trigger when a sidebar close button is clicked', async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <ControlTransportProvider transport={createPreviewTransport()}>
          <TooltipProvider>
            <PawSessionWorkspace
              record={liveSession()}
              recordId="session-click-close"
              onNewWork={vi.fn()}
              onSessionCreated={vi.fn()}
              onSessionUpdated={vi.fn()}
            />
          </TooltipProvider>
        </ControlTransportProvider>
      </QueryClientProvider>,
    );

    await screen.findByRole('textbox', { name: '消息' });
    const trigger = screen.getByRole('button', { name: 'Session 工具' });
    await user.click(trigger);
    await user.click(screen.getByRole('menuitem', { name: '任务与状态' }));
    const sidebar = screen.getByRole('complementary', { name: 'Session 工具侧栏' });
    await user.click(within(sidebar).getByRole('button', { name: '收起任务中心' }));

    expect(trigger).toHaveFocus();
  });

  it('projects the tool rail as a floating overlay so the message flow keeps the full viewport column', () => {
    // 浮层合同：桌面下侧栏绝对定位悬浮在对话之上；任何工具面板开启时，
    // 对话列仍然是唯一的网格列，绝不被挤出首屏。
    expect(agentMigratedCss).toMatch(
      /\.paw-desktop-root \.paw-session-workspace__side\s*\{[^}]*position:\s*absolute;/s,
    );
    expect(agentMigratedCss).toMatch(
      /\.paw-desktop-root \.paw-session-workspace\[data-panel='status'\] \.paw-session-workspace__body,\s*\.paw-desktop-root \.paw-session-workspace\[data-panel='subagents'\] \.paw-session-workspace__body,\s*\.paw-desktop-root \.paw-session-workspace\[data-panel='files'\] \.paw-session-workspace__body\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\);/s,
    );
    expect(appsCss).not.toMatch(/\.paw-session-workspace__body\s*\{[^}]*transition:[^;}]*grid-template-columns/s);
  });

  it('reserves the stop-button slot and status width so a turn starting never shifts the chrome row', () => {
    // 每次发送都会让乐观回合把 busy 翻真：停止钮随之挂载、状态词换词。
    // 稳定合同：状态词右缘钉住（min-width + 右对齐），停止钮在缺席时由同尺寸
    // 占位补上，视图切换与工具簇因此在回合开始/结束时纹丝不动。Room 同理。
    for (const owner of ['paw-session-workspace', 'paw-room-workspace']) {
      expect(appsCss).toMatch(new RegExp(
        `\\.${owner}__runtime > span \\{[^}]*min-width: calc\\(4em \\+ 12px\\);[^}]*justify-content: flex-end;`,
        's',
      ));
      expect(appsCss).toMatch(new RegExp(
        `\\.${owner}__runtime:not\\(:has\\(> button\\)\\)::after \\{[^}]*width: 29px;[^}]*height: 29px;`,
        's',
      ));
    }
  });

  it('dresses the portaled Session chrome in the OS window palette, not a private one', () => {
    // header 被 portal 进 .paw-window-titlebar 后就离开了 .paw-session-workspace
    // 的作用域，--paw-chat-* 取不到；它此前退回 v1 基线的暖褐色，在冷灰蓝的
    // 标题栏里显出第二种黑。--paw-chrome-* 定义在 .paw-desktop-root 上，portal
    // 之后仍然解析得到，是这条 chrome 唯一该说的色板。
    const chrome = agentMigratedCss.slice(
      agentMigratedCss.indexOf('.paw-desktop-root .paw-session-workspace__view-switch {'),
      agentMigratedCss.indexOf('.paw-desktop-root .paw-session-workspace__attention'),
    );
    expect(chrome).not.toBe('');
    expect(chrome).toContain('var(--paw-chrome-ink)');
    expect(chrome).toContain('var(--paw-chrome-muted)');
    for (const warm of ['rgb(42 28 0', '#7d7a75', '#2c2c2b', 'rgb(36 31 27']) {
      expect(chrome, warm).not.toContain(warm);
    }
  });

  it('uses one compact recoverable line when the Session has no files', async () => {
    const user = userEvent.setup();
    render(
      <ControlTransportProvider transport={createPreviewTransport()}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{ ...liveSession(), workspaceRoots: [] }}
            recordId="session-empty-files"
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    await screen.findByRole('textbox', { name: '消息' });
    await user.click(screen.getByRole('button', { name: 'Session 工具' }));
    await user.click(screen.getByRole('menuitem', { name: '文件' }));

    const sidebar = screen.getByRole('complementary', { name: 'Session 工具侧栏' });
    const empty = within(sidebar).getByRole('status');
    expect(empty).toHaveTextContent('当前没有文件；选择工作区目录后即可浏览。');
    expect(within(empty).getByRole('button', { name: '选择目录' })).toBeInTheDocument();
    expect(within(sidebar).queryByText('还没有工作区目录')).not.toBeInTheDocument();
  });

  it('coalesces a live streaming burst into bounded store commits without reordering events', async () => {
    const sessionId = 'session-stream';
    const transport = new StubControlTransport('mock', {
      'agent.session.snapshot': {
        messages: [{
          schemaVersion: 'rag-ime.agent-message.v1',
          id: 'user-stream',
          sessionId,
          turnId: 'turn-stream',
          role: 'user',
          status: 'completed',
          blocks: [{
            id: 'user-stream:text',
            type: 'text',
            status: 'completed',
            presentationKind: 'markdown',
            data: { text: '请流式生成一段较长的回答' },
          }],
          attachments: [],
          citations: [],
          createdAtMs: 1,
          completedAtMs: 1,
        }],
        liveEvents: [],
        lastSequence: 0,
        resumeToken: '',
        status: 'busy',
      },
      'agent.session.models': {},
      'agent.session.commands': {},
      'agent.tools.list': {},
      'agent.runtime.get': {},
    });
    useAgentLiveStore.getState().clear(sessionId);
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{ ...liveSession(), id: sessionId }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );
    await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(transport.subscriptionCount('agent.session.events')).toBe(1));

    const streamedText = (projection: AgentProjectionState | undefined): string => {
      const block = projection?.messagesById['turn-stream:assistant']?.blocks
        .find((candidate) => candidate.id === 'turn-stream:assistant:text');
      return typeof block?.data.text === 'string' ? block.data.text : '';
    };
    const commits: Array<{ text: string; hasTool: boolean }> = [];
    const unsubscribe = useAgentLiveStore.subscribe((state, previous) => {
      const current = state.projections[sessionId];
      if (current === previous.projections[sessionId]) return;
      commits.push({
        text: streamedText(current),
        hasTool: Object.keys(current?.activitiesById ?? {}).length > 0,
      });
    });

    const leadingDeltas = Array.from({ length: 20 }, (_, index) => `前段${index};`);
    const trailingDeltas = Array.from({ length: 20 }, (_, index) => `后段${index};`);
    act(() => {
      let sequence = 0;
      const emit = (eventType: string, payload: Record<string, unknown>) => {
        sequence += 1;
        transport.emit('agent.session.events', parseAgentEvent({
          schemaVersion: 'rag-ime.agent-event.v1',
          eventId: `${sessionId}:${sequence}`,
          sessionId,
          turnId: 'turn-stream',
          sequence,
          createdAtMs: sequence * 5,
          eventType,
          payload: {
            messageId: 'turn-stream:assistant',
            blockId: 'turn-stream:assistant:text',
            ...payload,
          },
          resumeToken: `${sessionId}:${sequence}`,
        }));
      };
      for (const delta of leadingDeltas) emit('text_delta', { delta });
      emit('tool_started', { toolCallId: 'call-stream-tool', toolName: 'overview' });
      for (const delta of trailingDeltas) emit('text_delta', { delta });
      emit('turn_completed', { status: 'completed' });
    });
    unsubscribe();

    // 42 Runtime events reach the store as exactly 4 commits: the tool event
    // flushes the 20 leading deltas before its own commit, and the terminal
    // event flushes the 20 trailing deltas the same way. Per-token React
    // render and layout passes are gone; the visible order never changes.
    expect(commits).toHaveLength(4);
    const toolCommit = commits.find((commit) => commit.hasTool);
    expect(toolCommit?.text).toBe(leadingDeltas.join(''));
    expect(streamedText(useAgentLiveStore.getState().projections[sessionId]))
      .toBe([...leadingDeltas, ...trailingDeltas].join(''));
    expect(useAgentLiveStore.getState().projections[sessionId]?.turnsById['turn-stream']?.status)
      .toBe('completed');
    useAgentLiveStore.getState().clear(sessionId);
  });

  it('quietly reconciles a final assistant message when the terminal SSE event is missed', async () => {
    const sessionId = 'session-terminal-gap';
    const turnId = 'turn-terminal-gap';
    const transport = new StubControlTransport('mock', {
      'agent.session.snapshot': {
        messages: [],
        liveEvents: [],
        lastSequence: 0,
        resumeToken: '',
        status: 'busy',
      },
      'agent.session.models': {},
      'agent.session.commands': {},
      'agent.tools.list': {},
      'agent.runtime.get': {},
    });
    useAgentLiveStore.getState().clear(sessionId);
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{ ...liveSession(), id: sessionId }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );
    await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(transport.subscriptionCount('agent.session.events')).toBe(1));
    const before = transport.requests.filter((request) => request.pathId === 'agent.session.snapshot').length;

    act(() => {
      transport.emit('agent.session.events', parseAgentEvent({
        schemaVersion: 'rag-ime.agent-event.v1',
        eventId: `${sessionId}:1`,
        sessionId,
        turnId,
        sequence: 1,
        createdAtMs: 10,
        eventType: 'message_completed',
        payload: {
          message: {
            schemaVersion: 'rag-ime.agent-message.v1',
            id: `${turnId}:assistant`,
            sessionId,
            turnId,
            role: 'assistant',
            status: 'completed',
            blocks: [{
              id: `${turnId}:assistant:text`,
              type: 'text',
              status: 'completed',
              presentationKind: 'markdown',
              data: { text: '已经完成。' },
            }],
            attachments: [],
            citations: [],
            createdAtMs: 10,
            completedAtMs: 10,
          },
        },
        resumeToken: `${sessionId}:1`,
      }));
    });

    await waitFor(() => expect(
      transport.requests.filter((request) => request.pathId === 'agent.session.snapshot').length,
    ).toBeGreaterThan(before));
    useAgentLiveStore.getState().clear(sessionId);
  });

  it('holds a follow-up beside the composer and gives it back when the turn is stopped', async () => {
    const sessionId = 'session-queue';
    const transport = busySessionTransport(sessionId);
    useAgentLiveStore.getState().clear(sessionId);
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{ ...liveSession(), id: sessionId }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(transport.subscriptionCount('agent.session.events')).toBe(1));
    act(() => { emitStreamDelta(transport, sessionId); });

    const user = userEvent.setup();
    await user.type(composer, '等这轮结束再看依赖图');
    await user.keyboard('{Enter}');

    expect(await screen.findByRole('status', { name: '等待当前执行完成后发送的消息' })).toHaveTextContent('等这轮结束再看依赖图');
    expect(composer).toHaveValue('');
    // Nothing was handed to Runtime: the hold is entirely reversible.
    expect(transport.requests.filter((request) => request.pathId === 'agent.session.prompt')).toEqual([]);
    expect(screen.queryByRole('radiogroup', { name: '消息投递方式' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '改为立即干预当前执行' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '停止本轮' }));
    await waitFor(() => expect(screen.getByRole('textbox', { name: '消息' })).toHaveValue('等这轮结束再看依赖图'));
    expect(screen.queryByRole('status', { name: '等待当前执行完成后发送的消息' })).not.toBeInTheDocument();
    expect(transport.requests.filter((request) => request.pathId === 'agent.session.prompt')).toEqual([]);
    useAgentLiveStore.getState().clear(sessionId);
  });

  it('does not turn an accepted Stop into a failure by awaiting the full archive', async () => {
    const sessionId = 'session-stop-does-not-await-history';
    let fullRequests = 0;
    const transport = new StubControlTransport('mock', {
      'agent.session.snapshot': (request: ControlRequest) => request.query?.view === 'recent'
        ? {
            messages: [],
            liveEvents: [parseAgentEvent({
              schemaVersion: 'rag-ime.agent-event.v1',
              eventId: `${sessionId}:1`,
              sessionId,
              turnId: 'turn-stop-fast',
              sequence: 1,
              createdAtMs: 1,
              eventType: 'tool_started',
              payload: { toolCallId: 'call-stop-fast', toolName: 'workspace_shell' },
              resumeToken: `${sessionId}:1`,
            })],
            lastSequence: 1,
            resumeToken: `${sessionId}:1`,
            status: 'busy',
            partial: true,
            snapshotScope: 'recent',
          }
        : (() => {
            fullRequests += 1;
            throw new Error('full archive temporarily unavailable');
          })(),
      'agent.session.models': {},
      'agent.session.commands': {},
      'agent.tools.list': {},
      'agent.runtime.get': {},
      'agent.session.abort': { ok: true },
    });
    useAgentLiveStore.getState().clear(sessionId);
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{ ...liveSession(), id: sessionId }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    await waitFor(() => expect(transport.subscriptionCount('agent.session.events')).toBe(1));
    await userEvent.setup().click(screen.getByRole('button', { name: '停止当前回合' }));
    await waitFor(() => expect(
      transport.requests.filter((request) => request.pathId === 'agent.session.abort'),
    ).toHaveLength(1));
    expect(fullRequests).toBe(0);
    expect(screen.queryByText(/Session 操作没有完成|full archive temporarily unavailable/)).not.toBeInTheDocument();
    useAgentLiveStore.getState().clear(sessionId);
  });

  it('sends exactly one held follow-up once the running turn settles', async () => {
    const sessionId = 'session-queue-drain';
    const transport = busySessionTransport(sessionId);
    useAgentLiveStore.getState().clear(sessionId);
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{ ...liveSession(), id: sessionId }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(transport.subscriptionCount('agent.session.events')).toBe(1));
    act(() => { emitStreamDelta(transport, sessionId); });

    const user = userEvent.setup();
    for (const text of ['第一条排队', '第二条排队']) {
      await user.type(composer, text);
      await user.keyboard('{Enter}');
    }
    expect(await screen.findByText('2 条排队中')).toBeInTheDocument();

    act(() => { emitTurnCompleted(transport, sessionId); });

    // Exactly one draft drains per settled turn, in the order it was held.
    await waitFor(() => expect(
      transport.requests.filter((request) => request.pathId === 'agent.session.prompt'),
    ).toHaveLength(1));
    expect(transport.requests.find((request) => request.pathId === 'agent.session.prompt')?.body)
      .toMatchObject({ message: '第一条排队' });
    await waitFor(() => expect(
      screen.getByRole('status', { name: '等待当前执行完成后发送的消息' }),
    ).toHaveTextContent('第二条排队'));
    useAgentLiveStore.getState().clear(sessionId);
  });

  it('lands the optimistic message and clears the draft at the click, before admission settles', async () => {
    const sessionId = 'session-instant-click';
    // The admission receipt never resolves inside this test: everything
    // asserted here must have happened synchronously with the click.
    const transport = new StubControlTransport('mock', {
      ...idleSessionRoutes(),
      'agent.session.prompt': () => new Promise(() => undefined),
    });
    useAgentLiveStore.getState().clear(sessionId);
    const user = userEvent.setup();
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{ ...liveSession(), id: sessionId }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    const composer = await screen.findByRole('textbox', { name: '消息' });
    await user.type(composer, '请开始这轮实现');
    await user.click(screen.getByRole('button', { name: '发送' }));

    const projection = useAgentLiveStore.getState().projections[sessionId];
    const optimisticIds = Object.values(projection?.optimisticByClientMessageId ?? {});
    expect(optimisticIds).toHaveLength(1);
    expect(projection?.messagesById[optimisticIds[0]!]?.blocks[0]?.data.text).toBe('请开始这轮实现');
    expect(screen.getByRole('textbox', { name: '消息' })).toHaveValue('');
    useAgentLiveStore.getState().clear(sessionId);
  });

  it('submits to a known Session without waiting for a stalled catalog reconciliation', async () => {
    const sessionId = 'session-known-direct-admission';
    const transport = new StubControlTransport('mock', {
      ...idleSessionRoutes(),
      'agent.sessions.list': () => new Promise(() => undefined),
      'agent.session.prompt': { ok: true },
    });
    useAgentLiveStore.getState().clear(sessionId);
    const user = userEvent.setup();
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{ ...liveSession(), id: sessionId }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    const composer = await screen.findByRole('textbox', { name: '消息' });
    await user.type(composer, '直接发送，不等目录');
    await user.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(
      transport.requests.filter((request) => request.pathId === 'agent.session.prompt'),
    ).toHaveLength(1));
    expect(transport.requests.filter((request) => request.pathId === 'agent.sessions.list')).toHaveLength(0);
    expect(transport.requests.find((request) => request.pathId === 'agent.session.prompt')?.body)
      .toMatchObject({ message: '直接发送，不等目录' });
    useAgentLiveStore.getState().clear(sessionId);
  });

  it('unlocks the composer once admission settles, without waiting for the quiet snapshot', async () => {
    const sessionId = 'session-instant-unlock';
    let snapshotCalls = 0;
    // The quiet post-send snapshot never resolves in this test; if sending
    // still gated on it (the old behavior), the composer would spin forever
    // and the unlock assertion below would time out.
    const transport = new StubControlTransport('mock', {
      ...idleSessionRoutes(),
      'agent.session.snapshot': () => {
        snapshotCalls += 1;
        return snapshotCalls === 1
          ? { messages: [], liveEvents: [], lastSequence: 0, resumeToken: '', status: 'active' }
          : new Promise(() => undefined);
      },
      'agent.session.prompt': { ok: true },
    });
    useAgentLiveStore.getState().clear(sessionId);
    const user = userEvent.setup();
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{ ...liveSession(), id: sessionId }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    const composer = await screen.findByRole('textbox', { name: '消息' });
    await user.type(composer, '请开始这轮实现');
    await user.click(screen.getByRole('button', { name: '发送' }));

    // The optimistic turn keeps the Session busy. A second draft must become
    // sendable as a held follow-up as soon as the admission receipt arrives;
    // it cannot wait for the unresolved quiet snapshot.
    await user.type(composer, '下一条');
    await waitFor(() => expect(
      screen.getByRole('button', { name: '排队，当前回合结束后发送' }),
    ).toBeEnabled());
    const projection = useAgentLiveStore.getState().projections[sessionId];
    const optimisticIds = Object.values(projection?.optimisticByClientMessageId ?? {});
    expect(optimisticIds).toHaveLength(1);
    // Admission succeeded: the optimistic message stays queued, never failed.
    expect(projection?.messagesById[optimisticIds[0]!]?.status).toBe('queued');
    // The background refresh was launched but is still unresolved: the unlock
    // above therefore cannot have waited for it.
    expect(snapshotCalls).toBe(2);
    useAgentLiveStore.getState().clear(sessionId);
  });

  it('supersedes a stale Steer rejection with one fresh prompt outside the old receipt lineage', async () => {
    const sessionId = 'session-stale-steer';
    let promptAttempt = 0;
    const snapshot = {
      messages: [{
        schemaVersion: 'rag-ime.agent-message.v1',
        id: `${sessionId}:user`,
        sessionId,
        turnId: 'turn-stale',
        role: 'user',
        status: 'completed',
        blocks: [{
          id: `${sessionId}:user:text`,
          type: 'text',
          status: 'completed',
          presentationKind: 'markdown',
          data: { text: '上一轮' },
        }],
        attachments: [],
        citations: [],
        createdAtMs: 1,
        completedAtMs: 1,
      }],
      liveEvents: [],
      lastSequence: 1,
      resumeToken: `${sessionId}:1`,
      status: 'busy',
    };
    const transport = new StubControlTransport('mock', {
      'agent.session.snapshot': snapshot,
      'agent.session.models': {},
      'agent.session.commands': {},
      'agent.tools.list': {},
      'agent.runtime.get': {},
      'agent.session.prompt': (request: ControlRequest) => {
        promptAttempt += 1;
        const body = request.body as Record<string, unknown>;
        if (promptAttempt === 1) {
          throw new ControlTransportHttpError(
            'agent.session.prompt',
            409,
            'Pi 当前没有可接收排队消息的活动回合',
            {
              ok: false,
              code: 'AGENT_COMMAND_FAILED',
              commandReceipt: {
                state: 'failed',
                clientMessageId: body.clientMessageId,
                causeCode: 'SESSION_IDLE',
              },
            },
          );
        }
        return { ok: true, accepted: true };
      },
    });
    useAgentLiveStore.getState().clear(sessionId);
    const user = userEvent.setup();
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{ ...liveSession(), id: sessionId }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    const composer = await screen.findByRole('textbox', { name: '消息' });
    await user.type(composer, '下一条普通消息');
    await user.keyboard('{Enter}');
    await user.click(screen.getByRole('button', { name: '改为立即干预当前执行' }));

    await waitFor(() => expect(
      transport.requests.filter((request) => request.pathId === 'agent.session.prompt'),
    ).toHaveLength(2));
    const prompts = transport.requests.filter((request) => request.pathId === 'agent.session.prompt');
    const first = prompts[0]?.body as Record<string, unknown>;
    const second = prompts[1]?.body as Record<string, unknown>;
    expect(first.delivery).toBe('steer');
    expect(second.delivery).toBeUndefined();
    expect(second.clientMessageId).not.toBe(first.clientMessageId);
    expect(second).not.toHaveProperty('retryOfClientMessageId');
    expect(second.message).toBe('下一条普通消息');
    expect(screen.queryByRole('button', { name: '重新同步' })).not.toBeInTheDocument();
    useAgentLiveStore.getState().clear(sessionId);
  });

  it('makes a live recent snapshot usable without starting a blocking full restore', async () => {
    const sessionId = 'session-recent-first';
    const fullSnapshot = new Promise(() => undefined);
    let fullRequests = 0;
    const transport = new StubControlTransport('mock', {
      'agent.session.snapshot': (request: ControlRequest) => request.query?.view === 'recent'
        ? {
            messages: [],
            liveEvents: [],
            lastSequence: 12,
            resumeToken: `${sessionId}:12`,
            status: 'active',
            partial: true,
            snapshotScope: 'recent',
          }
        : (() => {
            fullRequests += 1;
            return fullSnapshot;
          })(),
      'agent.session.models': {},
      'agent.session.commands': {},
      'agent.tools.list': {},
      'agent.runtime.get': {},
    });
    useAgentLiveStore.getState().clear(sessionId);
    const { container } = render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{ ...liveSession(), id: sessionId }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(container.querySelector('.paw-session-workspace__loading')).toBeNull());
    expect(fullRequests).toBe(0);
    expect(screen.getByText('最近上下文')).toBeInTheDocument();
    await waitFor(() => expect(transport.subscriptionCount('agent.session.events')).toBe(1));

    act(() => {
      transport.emit('agent.session.events', parseAgentEvent({
        schemaVersion: 'rag-ime.agent-event.v1',
        eventId: `${sessionId}:13`,
        sessionId,
        turnId: 'turn-recent-first',
        sequence: 13,
        createdAtMs: 13,
        eventType: 'turn_completed',
        payload: { status: 'completed' },
        resumeToken: `${sessionId}:13`,
      }));
    });
    expect(fullRequests).toBe(0);
    await userEvent.setup().click(screen.getByRole('button', { name: '加载完整记录' }));
    await waitFor(() => expect(fullRequests).toBe(1));
    useAgentLiveStore.getState().clear(sessionId);
  });

  it('subscribes and accepts a new send before an idle full archive resolves', async () => {
    const sessionId = 'session-idle-recent-first';
    let resolveFull: ((value: unknown) => void) | undefined;
    const fullSnapshot = new Promise<unknown>((resolve) => { resolveFull = resolve; });
    const recent = {
      messages: [{
        schemaVersion: 'rag-ime.agent-message.v1',
        id: `${sessionId}:assistant`,
        sessionId,
        turnId: 'turn-idle-history',
        role: 'assistant',
        status: 'completed',
        blocks: [{
          id: `${sessionId}:assistant:text`,
          type: 'text',
          status: 'completed',
          presentationKind: 'markdown',
          data: { text: '最近历史已可使用' },
        }],
        attachments: [],
        citations: [],
        createdAtMs: 1,
        completedAtMs: 2,
      }],
      liveEvents: [],
      lastSequence: 12,
      resumeToken: `${sessionId}:12`,
      status: 'idle',
      partial: true,
      snapshotScope: 'recent',
    };
    const transport = new StubControlTransport('mock', {
      'agent.session.snapshot': (request: ControlRequest) => (
        request.query?.view === 'recent' ? recent : fullSnapshot
      ),
      'agent.session.models': {},
      'agent.session.commands': {},
      'agent.tools.list': {},
      'agent.runtime.get': {},
      'agent.session.prompt': new Promise(() => undefined),
    });
    useAgentLiveStore.getState().clear(sessionId);
    const user = userEvent.setup();
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{ ...liveSession(), id: sessionId }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    const composer = await screen.findByRole('textbox', { name: '消息' });
    await waitFor(() => expect(transport.subscriptionCount('agent.session.events')).toBe(1));
    expect(transport.requests.filter((request) => (
      request.pathId === 'agent.session.snapshot' && request.query?.view === undefined
    ))).toHaveLength(0);
    await user.click(screen.getByRole('button', { name: '加载完整记录' }));
    expect(screen.getByRole('textbox', { name: '消息' })).toBe(composer);
    await user.type(composer, '完整历史还在恢复，但这一条必须立即发送');
    await user.keyboard('{Enter}');
    await waitFor(() => expect(
      transport.requests.some((request) => request.pathId === 'agent.session.prompt'),
    ).toBe(true));
    expect(Object.keys(
      useAgentLiveStore.getState().projections[sessionId]?.optimisticByClientMessageId ?? {},
    )).toHaveLength(1);

    await act(async () => {
      resolveFull?.({ ...recent, partial: false, snapshotScope: 'full' });
      await Promise.resolve();
    });
    expect(Object.keys(
      useAgentLiveStore.getState().projections[sessionId]?.optimisticByClientMessageId ?? {},
    )).toHaveLength(1);
    useAgentLiveStore.getState().clear(sessionId);
  });

  it.each(['turn_completed', 'turn_failed'] as const)(
    'settles the composer on %s before the deferred full snapshot resolves',
    async (eventType) => {
      const sessionId = `session-terminal-fast-${eventType}`;
      let fullRequests = 0;
      const transport = new StubControlTransport('mock', {
        'agent.session.snapshot': (request: ControlRequest) => request.query?.view === 'recent'
          ? {
              messages: [],
              liveEvents: [parseAgentEvent({
                schemaVersion: 'rag-ime.agent-event.v1',
                eventId: `${sessionId}:1`,
                sessionId,
                turnId: 'turn-terminal-fast',
                sequence: 1,
                createdAtMs: 1,
                eventType: 'tool_started',
                payload: { toolCallId: 'call-terminal-fast', toolName: 'workspace_shell' },
                resumeToken: `${sessionId}:1`,
              })],
              lastSequence: 1,
              resumeToken: `${sessionId}:1`,
              status: 'busy',
              partial: true,
              snapshotScope: 'recent',
            }
          : (() => {
              fullRequests += 1;
              return new Promise(() => undefined);
            })(),
        'agent.session.models': {},
        'agent.session.commands': {},
        'agent.tools.list': {},
        'agent.runtime.get': {},
      });
      useAgentLiveStore.getState().clear(sessionId);
      render(
        <ControlTransportProvider transport={transport}>
          <TooltipProvider>
            <PawSessionWorkspace
              record={{ ...liveSession(), id: sessionId }}
              recordId={sessionId}
              onNewWork={vi.fn()}
              onSessionCreated={vi.fn()}
              onSessionUpdated={vi.fn()}
            />
          </TooltipProvider>
        </ControlTransportProvider>,
      );

      expect(await screen.findByRole('button', { name: '停止当前回合' })).toBeVisible();
      await waitFor(() => expect(transport.subscriptionCount('agent.session.events')).toBe(1));
      expect(fullRequests).toBe(0);
      act(() => {
        transport.emit('agent.session.events', parseAgentEvent({
          schemaVersion: 'rag-ime.agent-event.v1',
          eventId: `${sessionId}:2`,
          sessionId,
          turnId: 'turn-terminal-fast',
          sequence: 2,
          createdAtMs: 2,
          eventType,
          payload: eventType === 'turn_failed'
            ? { status: 'failed', error: 'fixture failure' }
            : { status: 'completed' },
          resumeToken: `${sessionId}:2`,
        }));
      });

      await waitFor(() => expect(screen.queryByRole('button', { name: '停止当前回合' })).not.toBeInTheDocument());
      expect(screen.getByRole('textbox', { name: '消息' })).toBeEnabled();
      expect(fullRequests).toBe(0);
      useAgentLiveStore.getState().clear(sessionId);
    },
  );

  it('keeps cached durable history visible while a live recent snapshot subscribes', async () => {
    const sessionId = 'session-recent-preserves-cache';
    useAgentLiveStore.getState().clear(sessionId);
    useAgentLiveStore.getState().hydrate(sessionId, {
      messages: [{
        schemaVersion: 'rag-ime.agent-message.v1',
        id: `${sessionId}:assistant`,
        sessionId,
        turnId: 'turn-cached',
        role: 'assistant',
        status: 'completed',
        blocks: [{
          id: `${sessionId}:assistant:text`,
          type: 'text',
          status: 'completed',
          presentationKind: 'markdown',
          data: { text: '今天已经完成的持久历史' },
        }],
        attachments: [],
        citations: [],
        createdAtMs: 1,
        completedAtMs: 2,
      }],
      liveEvents: [],
      lastSequence: 12,
      resumeToken: `${sessionId}:12`,
      status: 'active',
    });
    const fullSnapshot = new Promise(() => undefined);
    const transport = new StubControlTransport('mock', {
      'agent.session.snapshot': (request: ControlRequest) => request.query?.view === 'recent'
        ? {
            messages: [],
            liveEvents: [],
            lastSequence: 13,
            resumeToken: `${sessionId}:13`,
            status: 'active',
            partial: true,
            snapshotScope: 'recent',
          }
        : fullSnapshot,
      'agent.session.models': {},
      'agent.session.commands': {},
      'agent.tools.list': {},
      'agent.runtime.get': {},
    });

    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{ ...liveSession(), id: sessionId }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    expect(await screen.findByText('今天已经完成的持久历史')).toBeVisible();
    expect(screen.getByText('最近上下文')).toBeInTheDocument();
    useAgentLiveStore.getState().clear(sessionId);
  });

  it('settles a cached stale turn from a quiescent recent snapshot without awaiting full history', async () => {
    const sessionId = 'session-recent-quiescent-cache';
    let fullRequests = 0;
    useAgentLiveStore.getState().clear(sessionId);
    useAgentLiveStore.getState().hydrate(sessionId, {
      messages: [{
        schemaVersion: 'rag-ime.agent-message.v1',
        id: `${sessionId}:user`,
        sessionId,
        turnId: 'turn-stale-stop',
        role: 'user',
        status: 'completed',
        blocks: [{
          id: `${sessionId}:user:text`,
          type: 'text',
          status: 'completed',
          presentationKind: 'markdown',
          data: { text: '停止前仍需保留的历史' },
        }],
        attachments: [],
        citations: [],
        createdAtMs: 1,
        completedAtMs: 1,
      }],
      liveEvents: [parseAgentEvent({
        schemaVersion: 'rag-ime.agent-event.v1',
        eventId: `${sessionId}:1`,
        sessionId,
        turnId: 'turn-stale-stop',
        sequence: 1,
        createdAtMs: 2,
        eventType: 'tool_started',
        payload: { toolCallId: 'call-stale-stop', toolName: 'workspace_shell' },
        resumeToken: `${sessionId}:1`,
      })],
      lastSequence: 1,
      resumeToken: `${sessionId}:1`,
      status: 'busy',
    });
    const transport = new StubControlTransport('mock', {
      'agent.session.snapshot': (request: ControlRequest) => request.query?.view === 'recent'
        ? {
            messages: [],
            liveEvents: [],
            lastSequence: 2,
            resumeToken: `${sessionId}:2`,
            status: 'idle',
            partial: true,
            snapshotScope: 'recent',
            runtimeQuiescent: true,
          }
        : (() => {
            fullRequests += 1;
            return new Promise(() => undefined);
          })(),
      'agent.session.models': {},
      'agent.session.commands': {},
      'agent.tools.list': {},
      'agent.runtime.get': {},
    });

    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{ ...liveSession(), id: sessionId }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    expect(await screen.findByText('停止前仍需保留的历史')).toBeVisible();
    await waitFor(() => expect(screen.queryByRole('button', { name: '停止当前回合' })).not.toBeInTheDocument());
    expect(screen.getByRole('textbox', { name: '消息' })).toBeEnabled();
    expect(fullRequests).toBe(0);
    await waitFor(() => expect(transport.subscriptionCount('agent.session.events')).toBe(1));
    useAgentLiveStore.getState().clear(sessionId);
  });

  it('gives a failed prompt exactly one failure surface with its own recovery', async () => {
    const sessionId = 'session-prompt-failure';
    const transport = idleSessionTransport();
    useAgentLiveStore.getState().clear(sessionId);
    const user = userEvent.setup();
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{ ...liveSession(), id: sessionId }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    const composer = await screen.findByRole('textbox', { name: '消息' });
    await user.type(composer, '请开始这轮实现');
    await user.click(screen.getByRole('button', { name: '发送' }));

    // FailOptimistic owns the turn. This file does not mock Virtuoso geometry,
    // so the turn card itself is covered in agent-feature tests; here we lock
    // the PAWOS-specific bug: no second 重新同步 banner for the same failure.
    await waitFor(() => {
      const projection = useAgentLiveStore.getState().projections[sessionId];
      expect(projection?.turnOrder.some((turnId) => (
        projection.turnsById[turnId]?.status === 'failed'
      ))).toBe(true);
    });
    expect(transport.requests.some((request) => request.pathId === 'agent.session.prompt')).toBe(true);
    expect(document.querySelector('.paw-session-workspace__error')).toBeNull();
    expect(screen.queryByRole('button', { name: '重新同步' })).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '消息' })).toHaveValue('请开始这轮实现');
    useAgentLiveStore.getState().clear(sessionId);
  });

  it('rolls back a command fingerprint conflict instead of inventing a failed turn', async () => {
    const sessionId = 'session-command-fingerprint-conflict';
    const transport = new StubControlTransport('mock', {
      ...idleSessionRoutes(),
      'agent.session.prompt': (request: ControlRequest) => {
        const body = request.body as Record<string, unknown>;
        throw new ControlTransportHttpError(
          'agent.session.prompt',
          409,
          'command fingerprint mismatch',
          {
            ok: false,
            code: 'AGENT_COMMAND_CONFLICT',
            commandReceipt: {
              state: 'conflict',
              clientMessageId: body.clientMessageId,
              causeCode: 'COMMAND_FINGERPRINT_MISMATCH',
              recoveryState: 'new_command_required',
            },
          },
        );
      },
    });
    useAgentLiveStore.getState().clear(sessionId);
    const user = userEvent.setup();
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{ ...liveSession(), id: sessionId }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    const composer = await screen.findByRole('textbox', { name: '消息' });
    await user.type(composer, '内容已变化，保留重发');
    await user.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(composer).toHaveValue('内容已变化，保留重发'));
    const projection = useAgentLiveStore.getState().projections[sessionId];
    expect(Object.keys(projection?.optimisticByClientMessageId ?? {})).toHaveLength(0);
    expect(projection?.turnOrder.some((turnId) => projection.turnsById[turnId]?.status === 'failed')).toBe(false);
    expect(screen.getByText('这次发送内容已经变化，输入已保留；请直接重新发送一次。')).toBeVisible();
    useAgentLiveStore.getState().clear(sessionId);
  });

  it.each([false, true])('continues a durably accepted failed turn without replaying its input (screen: %s)', async (withScreen) => {
    const sessionId = 'session-accepted-turn-retry';
    const promptRequests: ControlRequest[] = [];
    const context = { mediaId: 'media_abcdefghijklmnop', sourceAppBundleId: 'com.example.Editor', capturedAtMs: 1000 };
    const transport = new StubControlTransport('mock', {
      'agent.session.snapshot': {
        messages: [{
          schemaVersion: 'rag-ime.agent-message.v1',
          id: `${sessionId}:user`,
          sessionId,
          turnId: 'turn-failed',
          role: 'user',
          status: 'completed',
          clientMessageId: 'client-accepted-root',
          blocks: [{
            id: `${sessionId}:user:text`,
            type: 'text',
            status: 'completed',
            presentationKind: 'markdown',
            data: { text: '查询本月经营数据' },
          }],
          attachments: withScreen ? [context.mediaId] : [],
          citations: [],
          createdAtMs: 1,
          completedAtMs: 1,
        }],
        liveEvents: [],
        lastSequence: 1,
        resumeToken: `${sessionId}:1`,
        status: 'idle',
      },
      'agent.session.models': {},
      'agent.session.commands': {},
      'agent.tools.list': {},
      'agent.runtime.get': {},
      'agent.session.prompt': (request: ControlRequest) => {
        promptRequests.push(request);
        const body = request.body as Record<string, unknown>;
        if (body.retryOfClientMessageId) {
          throw new ControlTransportHttpError(
            'agent.session.prompt',
            409,
            'only a durably failed command may have a successor',
            {
              ok: false,
              code: 'AGENT_COMMAND_CONFLICT',
              commandReceipt: {
                state: 'conflict',
                clientMessageId: body.clientMessageId,
              },
            },
          );
        }
        return new Promise(() => undefined);
      },
    });
    useAgentLiveStore.getState().clear(sessionId);
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{ ...liveSession(), id: sessionId }}
            recordId={sessionId}
            screenContext={withScreen ? context : undefined}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    const retry = await screen.findByRole('button', { name: '继续' });
    fireEvent.click(retry);
    fireEvent.click(retry);

    await waitFor(() => expect(promptRequests).toHaveLength(1));
    expect(promptRequests[0]?.body).toMatchObject({
      message: '继续。请基于当前 Session 已保留的工具结果和文件生成最终回复，不要重试或重复已经完成的操作；如果仍缺少信息，明确说明下一步。',
      attachments: [],
    });
    expect(promptRequests[0]?.body).not.toHaveProperty('retryOfClientMessageId');
    if (withScreen) expect(promptRequests[0]?.body).toMatchObject({ screenContext: context });
    useAgentLiveStore.getState().clear(sessionId);
  });
  it('rolls back a rejected admission retry card when admission becomes unresolved before submission', async () => {
    const sessionId = 'session-unresolved-retry-rollback';
    const pendingSessionRefresh = deferred<unknown>();
    let holdSessionRefresh = false;
    const transport = new StubControlTransport('mock', {
      'agent.sessions.list': () => holdSessionRefresh
        ? pendingSessionRefresh.promise
        : { ok: true, items: [{ ...liveSession(), id: sessionId }] },
      'agent.session.snapshot': {
        messages: [],
        liveEvents: [],
        lastSequence: 1,
        resumeToken: `${sessionId}:1`,
        status: 'idle',
      },
      'agent.session.models': {},
      'agent.session.commands': {},
      'agent.tools.list': {},
      'agent.runtime.get': {},
      'agent.session.prompt': { ok: true },
    });
    useAgentLiveStore.getState().clear(sessionId);
    const user = userEvent.setup();
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{ ...liveSession(), id: sessionId }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    await screen.findByRole('textbox', { name: '消息' });
    act(() => {
      const store = useAgentLiveStore.getState();
      store.appendOptimistic(sessionId, {
        clientMessageId: 'client-unresolved-retry', text: '这条消息需要安全重试',
        attachments: [], nowMs: Date.now(),
      });
      store.failOptimistic(sessionId, 'client-unresolved-retry', 'prompt rejected', Date.now());
    });

    const retry = await screen.findByRole('button', { name: '重试本轮' });
    const projection = useAgentLiveStore.getState().projections[sessionId]!;
    const userMessage = projection.messagesById['local:client-unresolved-retry']!;
    const sessionListRequestsBeforeRetry = transport.requests.filter(
      (request) => request.pathId === 'agent.sessions.list',
    ).length;
    holdSessionRefresh = true;
    await user.click(retry);
    await waitFor(() => expect(transport.requests.filter(
      (request) => request.pathId === 'agent.sessions.list',
    )).toHaveLength(sessionListRequestsBeforeRetry + 1));

    userMessage.admissionState = 'unresolved';
    pendingSessionRefresh.resolve({
      ok: true,
      items: [{ ...liveSession(), id: sessionId }],
    });
    expect(await screen.findByText('这条消息仍无法确认是否已执行；为避免重复执行，不能自动重试。请先重新同步 Session。')).toBeInTheDocument();
    expect(transport.requests.filter((request) => request.pathId === 'agent.session.prompt')).toHaveLength(0);

    userMessage.admissionState = undefined;
    await user.type(screen.getByRole('textbox', { name: '消息' }), '触发重新渲染');
    const rolledBackRetry = await screen.findByRole('button', { name: '重试本轮' });
    expect(rolledBackRetry).toBeEnabled();
    expect(screen.queryByRole('button', { name: '已提交重试' })).not.toBeInTheDocument();
    useAgentLiveStore.getState().clear(sessionId);
  });


  it('offers the current Session to Trace Agent from the shared error alert', async () => {
    const sessionId = 'session-snapshot-failure';
    const transport = new StubControlTransport('mock', {
      'agent.session.snapshot': () => { throw new Error('snapshot unavailable'); },
      'agent.session.models': {},
      'agent.session.commands': {},
      'agent.tools.list': {},
      'agent.runtime.get': {},
    });
    const routes: string[] = [];
    render(
      <PawOsDesktopProvider openRoute={(route) => routes.push(route)} openWindow={() => undefined}>
        <ControlTransportProvider transport={transport}>
          <TooltipProvider>
            <PawSessionWorkspace
              record={{ ...liveSession(), id: sessionId }}
              recordId={sessionId}
              onNewWork={vi.fn()}
              onSessionCreated={vi.fn()}
              onSessionUpdated={vi.fn()}
            />
          </TooltipProvider>
        </ControlTransportProvider>
      </PawOsDesktopProvider>,
    );

    expect(await screen.findByRole('alert')).toHaveTextContent('连接暂时不可用，系统会继续自动重连。');
    await userEvent.setup().click(screen.getByRole('button', { name: '交给 Trace Agent' }));
    const handoff = parseTraceAgentHandoff(routes[0]?.split('?', 2)[1] ?? '');
    expect(handoff).toMatchObject({
      kind: 'session',
      entityId: `session:${sessionId}:error`,
      sessionId,
      sourceRoute: `/agent?session=${sessionId}`,
      refs: { surface: 'session-workspace' },
    });
  });

  it('keeps workspace-managed permission updates scoped and confirms the selected roots', async () => {
    const sessionId = 'session-managed-permission';
    const transport = new StubControlTransport('mock', {
      ...idleSessionRoutes(),
      'agent.session.mode.update': (request: ControlRequest) => {
        const body = request.body as Record<string, unknown>;
        return {
          ok: true,
          session: {
            ...liveSession(),
            id: sessionId,
            executionMode: body.executionMode,
            toolProfileVersion: body.toolProfileVersion,
            workspaceRoots: body.workspaceRoots,
          },
        };
      },
    });
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{
              ...liveSession(),
              id: sessionId,
              workspaceRoots: ['/work/paw'],
            }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '对话权限：写入与命令确认' }));
    const picker = document.querySelector('.agent-picker-popover');
    expect(picker).not.toBeNull();
    await user.click(within(picker as HTMLElement).getByRole('radio', { name: /^工作区托管/ }));

    await waitFor(() => expect(transport.requests.find((request) => (
      request.pathId === 'agent.session.mode.update'
    ))).toMatchObject({
      body: {
        mode: 'coordinator',
        executionMode: 'workspace_managed',
        workspaceRoots: ['/work/paw'],
        toolProfileVersion: 'control-center-v1',
        toolAllowlistMode: 'profile',
        workspaceScopeConfirmation: 'APPROVE_WORKSPACE_SCOPE',
      },
    }));
    useAgentLiveStore.getState().clear(sessionId);
  });

  it('rebinds a managed Session through the installed Electron directory picker', async () => {
    const sessionId = 'session-managed-electron';
    const transport = new StubControlTransport('mock', {
      ...idleSessionRoutes(),
      'agent.session.mode.update': (request: ControlRequest) => ({
        ok: true,
        session: {
          ...liveSession(),
          id: sessionId,
          ...(request.body as Record<string, unknown>),
        },
      }),
    });
    Object.defineProperty(transport, 'pickFiles', { configurable: true, value: undefined });
    const pickWorkspaceDirectory = vi.fn(async () => ({
      name: 'paw-next',
      path: '/work/paw-next',
    }));
    window.pawBrowserHost = {
      kind: 'electron-webview',
      partition: 'persist:paw-browser',
      pickWorkspaceDirectory,
    } as unknown as NonNullable<typeof window.pawBrowserHost>;
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{
              ...liveSession(),
              id: sessionId,
              executionMode: 'workspace_managed',
              toolProfileVersion: 'control-center-v1',
              workspaceRoots: ['/work/paw-old'],
            }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '对话权限：工作区托管（沙箱）' }));
    const workspace = screen.getByRole('region', { name: '授权工作区' });
    await user.click(within(workspace).getByRole('button', { name: '更改目录' }));

    await waitFor(() => expect(pickWorkspaceDirectory).toHaveBeenCalledTimes(1));
    expect(transport.requests.find((request) => (
      request.pathId === 'agent.session.mode.update'
    ))).toMatchObject({
      body: {
        mode: 'coordinator',
        executionMode: 'workspace_managed',
        workspaceRoots: ['/work/paw-next'],
        toolProfileVersion: 'control-center-v1',
        toolAllowlistMode: 'profile',
        workspaceScopeConfirmation: 'APPROVE_WORKSPACE_SCOPE',
      },
    });
    useAgentLiveStore.getState().clear(sessionId);
  });

  it('offers an explicit workspace replacement instead of resync for a removed Session directory', async () => {
    const sessionId = 'session-removed-workspace';
    const transport = new StubControlTransport('native', {
      'agent.session.snapshot': () => {
        throw new ControlTransportHttpError(
          'agent.session.snapshot',
          409,
          'session workspace is no longer available',
          {
            ok: false,
            errorCode: 'session_workspace_missing',
            retryable: false,
            recovery: { action: 'select_workspace' },
          },
        );
      },
      'agent.session.models': {},
      'agent.session.commands': {},
      'agent.tools.list': {},
      'agent.runtime.get': {},
      'agent.session.mode.update': {
        ok: true,
        session: {
          ...liveSession(),
          id: sessionId,
          workspaceRoots: ['/work/rebound'],
        },
      },
    });
    Object.assign(transport, {
      pickFiles: vi.fn().mockResolvedValue([{
        id: 'workspace-rebound',
        name: 'rebound',
        path: '/work/rebound',
        mimeType: 'application/x-directory',
        byteSize: 0,
      }]),
    });
    render(
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawSessionWorkspace
            record={{
              ...liveSession(),
              id: sessionId,
              mode: 'coordinator',
              workspaceRoots: ['/work/removed'],
            }}
            recordId={sessionId}
            onNewWork={vi.fn()}
            onSessionCreated={vi.fn()}
            onSessionUpdated={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>,
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(
      '这个 Session 的工作目录已不存在',
    );
    expect(screen.queryByRole('button', { name: '重新同步' })).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole('button', { name: '选择工作目录' }));
    expect(transport.requests.find((request) => (
      request.pathId === 'agent.session.mode.update'
    ))).toMatchObject({
      body: { workspaceRoots: ['/work/rebound', '/'] },
    });
  });

  it('uses one compact on-demand row when the Session has no subagents', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const transport = new StubControlTransport('mock', {
      'agent.subagents.list': { ok: true, items: [] },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <ControlTransportProvider transport={transport}>
          <TooltipProvider>
            <SessionSubagentPanel
              compactEmpty
              open
              session={liveSession()}
              sessionId="session-no-subagents"
              tools={[]}
              onClose={vi.fn()}
            />
          </TooltipProvider>
        </ControlTransportProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText('还没有子 Agent')).toBeVisible();
    expect(screen.getByText('配置子 Agent')).toBeVisible();
    expect(screen.queryByRole('region', { name: '子 Agent 运行图' })).not.toBeInTheDocument();
  });

});

/** A Session whose snapshot opens on one running turn, so the composer offers
 *  the busy delivery choices a queued follow-up competes with. */
function busySessionTransport(sessionId: string): StubControlTransport {
  return new StubControlTransport('mock', {
    'agent.session.snapshot': {
      messages: [{
        schemaVersion: 'rag-ime.agent-message.v1',
        id: `${sessionId}:user`,
        sessionId,
        turnId: 'turn-busy',
        role: 'user',
        status: 'completed',
        blocks: [{
          id: `${sessionId}:user:text`,
          type: 'text',
          status: 'completed',
          presentationKind: 'markdown',
          data: { text: '请开始这轮实现' },
        }],
        attachments: [],
        citations: [],
        createdAtMs: 1,
        completedAtMs: 1,
      }],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'busy',
    },
    'agent.session.models': {},
    'agent.session.commands': {},
    'agent.tools.list': {},
    'agent.runtime.get': {},
    'agent.session.prompt': { ok: true },
    'agent.session.abort': { ok: true },
  });
}

/** Route table for an idle Session with no prompt behavior chosen yet. */
function idleSessionRoutes(): ConstructorParameters<typeof StubControlTransport>[1] {
  return {
    'agent.session.snapshot': {
      messages: [],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'active',
    },
    'agent.session.models': {},
    'agent.session.commands': {},
    'agent.tools.list': {},
    'agent.runtime.get': {},
  };
}

/** An idle Session whose Runtime rejects the prompt, so submitting produces one
 *  real failed turn instead of a stub acknowledgement. */
function idleSessionTransport(): StubControlTransport {
  return new StubControlTransport('mock', {
    ...idleSessionRoutes(),
    'agent.session.prompt': () => {
      throw new Error('provider_request_failed');
    },
  });
}

function emitStreamDelta(transport: StubControlTransport, sessionId: string): void {
  transport.emit('agent.session.events', parseAgentEvent({
    schemaVersion: 'rag-ime.agent-event.v1',
    eventId: `${sessionId}:1`,
    sessionId,
    turnId: 'turn-busy',
    sequence: 1,
    createdAtMs: 5,
    eventType: 'text_delta',
    payload: {
      messageId: 'turn-busy:assistant',
      blockId: 'turn-busy:assistant:text',
      delta: '正在推进…',
    },
    resumeToken: `${sessionId}:1`,
  }));
}

function emitTurnCompleted(transport: StubControlTransport, sessionId: string): void {
  transport.emit('agent.session.events', parseAgentEvent({
    schemaVersion: 'rag-ime.agent-event.v1',
    eventId: `${sessionId}:2`,
    sessionId,
    turnId: 'turn-busy',
    sequence: 2,
    createdAtMs: 10,
    eventType: 'turn_completed',
    payload: { messageId: 'turn-busy:assistant', status: 'completed' },
    resumeToken: `${sessionId}:2`,
  }));
}

function liveSession(): SessionSummary {
  return {
    id: 'session-live',
    title: '完整迁移',
    mode: 'coordinator',
    status: 'active',
    roleId: 'builder',
    roleVersion: '1',
    roleBookRevisionId: '',
    updatedAtMs: 170,
    workspaceRoots: ['/Users/example/personal-agent-workbench'],
    executionMode: 'per_action',
    modelProfile: 'openai/gpt-5.6-sol',
  };
}

function setDocumentVisibility(state: 'hidden' | 'visible'): void {
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    value: state,
  });
  document.dispatchEvent(new Event('visibilitychange'));
}
function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}
