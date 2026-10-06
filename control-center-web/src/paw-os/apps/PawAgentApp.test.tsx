import { notifyManager, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import type { RoomSummary, RoomWorkItem } from '@/features/rooms/room-types';
import type { SessionSummary } from '@/features/agent/types';
import type { WorkspaceComposerHeaderView } from './PawSessionWorkspace';
import { MockControlTransport } from '@/test/mock-transport';
import type { ControlRequest } from '@/platform/transport';
import { PawOsAppSurfaceProvider, PawOsDesktopProvider } from '@/features/paw-os/surface-context';
import { parseTraceAgentHandoff } from '@/features/trace-agent/handoff';
import type { PawOsWindowTarget } from '@/features/paw-os/model/desktop';
import { PawAgentApp } from './PawAgentApp';
import { agentModeStore } from '@/features/semantic-workspace/agent-mode-store';
import { useRoomLiveStore } from '@/features/rooms/state/live-store';
import { parseRoomEvent } from '@/contracts/validators';

const workspaceEvaluations = vi.hoisted(() => ({ session: 0, room: 0 }));
const workspaceLoading = vi.hoisted(() => ({ room: null as Promise<void> | null }));

vi.mock('./PawSessionWorkspace', () => {
  workspaceEvaluations.session += 1;
  return {
    PawSessionWorkspace: ({ record, recordId, recordMetadataKnown, initialDraft, initialSubmission, renderComposerHeader }: { recordMetadataKnown?: boolean; record?: SessionSummary; recordId: string; initialDraft?: string; initialSubmission?: { clientMessageId: string; message: string }; renderComposerHeader?: (view: WorkspaceComposerHeaderView) => ReactNode }) => (
      <div>
        Session 工作区
        <output data-testid="session-record-id">{record?.id ?? `missing:${recordId}`}</output>
        <output data-testid="session-record-known">{String(recordMetadataKnown)}</output>
        <output data-testid="session-record-read-only">{record?.evaluationSnapshot ? 'true' : 'false'}</output>
        <output data-testid="session-initial-submission">{JSON.stringify(initialSubmission)}</output>
        <output data-testid="session-initial-draft">{initialDraft}</output>
        {record && renderComposerHeader?.({ session: record, draft: '检查具体目标', disabled: false, sourceMessageId: () => 'source-cutoff' })}
      </div>
    ),
  };
});
vi.mock('./PawRoomWorkspace', () => {
  workspaceEvaluations.room += 1;
  return {
    PawRoomWorkspace: ({ initialDraft, record, recordId, onRoomUpdated }: {
      initialDraft?: string; record?: RoomSummary; recordId: string; onRoomUpdated: (room: RoomSummary) => void;
    }) => {
      if (workspaceLoading.room) throw workspaceLoading.room;
      return <div>
        Room 工作区 · {recordId}<output data-testid="room-initial-draft">{initialDraft}</output>
        <output data-testid="room-record-title">{record?.title ?? 'missing'}</output>
        <button onClick={() => onRoomUpdated(roomFixture({ id: recordId, title: '恢复的协作主对话' }))}>接收 Room 快照</button>
      </div>;
    },
  };
});
vi.mock('@/features/roles', () => ({ RolesFeature: () => <div>角色工作区</div> }));

// These are the retained traditional workflows; first-use Jev is covered in
// PawAgentModes and PawAgentHome with no stored preference.
beforeEach(() => {
  // React Query explicitly supports an act notification wrapper for tests.
  // Keep its async scheduler and all real request/transition boundaries.
  notifyManager.setNotifyFunction(callback => { act(callback); });
  agentModeStore.select('traditional');
});
afterEach(() => {
  cleanup();
  notifyManager.setNotifyFunction(callback => { callback(); });
  useRoomLiveStore.getState().reset();
  delete window.pawBrowserHost;
});

describe('PAWOS Agent App', () => {
  // Keep the cold-load contract before tests that intentionally open a Session.
  it('evaluates only the selected workspace and preserves route identity and draft across Room → Session → Room', async () => {
    const transport = createTransport();
    const mode = deferred<unknown>();
    const originalRequest = transport.request.bind(transport);
    transport.request = <T,>(request: ControlRequest): Promise<T> => request.pathId === 'agent.jev.get'
      ? mode.promise as Promise<T> : originalRequest<T>(request);
    const roomRoute = '/agent?room=room-old&draft=room%20draft';
    const view = renderAgent(transport, { initialRoute: roomRoute });
    try {
      await waitFor(() => expect({ ...workspaceEvaluations }).toEqual({ session: 0, room: 1 }));
      // Prewarming does not choose a JEV mode or mount the Room early.
      expect(screen.queryByText('Room 工作区 · room-old')).not.toBeInTheDocument();
    } finally { await act(async () => mode.resolve({ ok: true, mode: 'jev', items: [] })); }

    expect(await screen.findByText('Room 工作区 · room-old')).toBeInTheDocument();
    expect(screen.getByTestId('room-initial-draft')).toHaveTextContent('room draft');
    expect(workspaceEvaluations).toEqual({ session: 0, room: 1 });

    view.rerender(agentTree(transport, { initialRoute: '/agent?session=session-old' }));
    expect(await screen.findByText('Session 工作区')).toBeInTheDocument();
    expect(screen.getByTestId('session-record-id')).toHaveTextContent('session-old');
    expect(workspaceEvaluations).toEqual({ session: 1, room: 1 });

    view.rerender(agentTree(transport, { initialRoute: roomRoute }));
    expect(await screen.findByText('Room 工作区 · room-old')).toBeInTheDocument();
    expect(screen.getByTestId('room-initial-draft')).toHaveTextContent('room draft');
    expect(workspaceEvaluations).toEqual({ session: 1, room: 1 });
  });

  it.each([false, true])('retains a task form while inspecting an old task only in its original transport (replacement: %s)', async replacement => {
    const oldTask = { id: 'task-existing', title: '已有任务', mode: 'assistant', status: 'idle', updatedAtMs: 2, workspaceRoots: ['/work/demo'], metadata: { primaryTask: true } };
    const transport = createTransport({ primaryTasks: [oldTask] });
    const view = renderAgent(transport, { initialRoute: '/agent' });
    await waitFor(() => expect(screen.getByRole('button', { name: /进入对话/ })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: /进入对话/ }));
    fireEvent.click(await screen.findByRole('button', { name: '交给助手做' }));
    const message = await screen.findByRole('textbox', { name: '和我的助手聊聊' });
    fireEvent.change(message, { target: { value: '待确认的新目标' } });
    fireEvent.change(screen.getByRole('textbox', { name: '本次工作目录' }), { target: { value: '/work/draft' } });
    fireEvent.change(screen.getByRole('textbox', { name: '完成标准' }), { target: { value: '完成三项检查' } });
    fireEvent.click(screen.getByRole('checkbox'));
    if (!replacement) {
      await waitFor(() => expect(screen.getByRole('button', { name: '授权并开始任务' })).toBeEnabled());
      fireEvent.click(screen.getByRole('button', { name: '授权并开始任务' }));
      await screen.findByText(/草稿已保留；重试会核对同一次请求/);
    }
    fireEvent.click(await screen.findByRole('button', { name: /已有任务/ }));
    expect(await screen.findByTestId('session-record-id')).toHaveTextContent('task-existing');
    expect(screen.getByTestId('session-initial-draft')).toBeEmptyDOMElement();
    if (replacement) view.rerender(agentTree(createTransport({ primaryTasks: [oldTask] }), { initialRoute: '/agent?session=task-existing' }));
    fireEvent.click(screen.getByRole('button', { name: '返回我的助手' }));
    const restored = await screen.findByRole('textbox', { name: '和我的助手聊聊' });
    if (replacement) {
      expect(restored).toHaveValue('');
      expect(screen.getByRole('button', { name: '聊一聊' })).toHaveAttribute('aria-pressed', 'true');
      expect(screen.queryByRole('textbox', { name: '本次工作目录' })).not.toBeInTheDocument();
    } else {
      expect(restored).toHaveValue('待确认的新目标');
      expect(screen.getByRole('textbox', { name: '本次工作目录' })).toHaveValue('/work/draft');
      expect(screen.getByRole('textbox', { name: '完成标准' })).toHaveValue('完成三项检查');
      expect(screen.getByRole('checkbox')).toBeChecked();
      await waitFor(() => expect(screen.getByRole('button', { name: '授权并开始任务' })).toBeEnabled());
      fireEvent.click(screen.getByRole('button', { name: '授权并开始任务' }));
      await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'agent.primary.tasks.create')).toHaveLength(2));
      const attempts = transport.requests.filter(({ request }) => request.pathId === 'agent.primary.tasks.create');
      expect(attempts[0].request.body).toMatchObject({ sourceSessionId: 'primary', sourceMessageId: 'source-cutoff', objective: '待确认的新目标', acceptanceCriteria: ['完成三项检查'] });
      expect(attempts[1].request.body).toEqual(attempts[0].request.body);
    }
  });

  it('preserves an unsubmitted Home task when leaving through the parent history sidebar', async () => {
    const user = userEvent.setup();
    renderAgent(createTransport(), { initialRoute: '/agent' });
    await waitFor(() => {
      expect(document.querySelector('.paw-agent-recents')).toBeInTheDocument();
      expect(document.querySelector('.paw-agent-recents')).not.toHaveAttribute('aria-busy', 'true');
    });
    await user.click(screen.getByRole('button', { name: '交给助手做' }));
    fireEvent.change(screen.getByRole('textbox', { name: '和我的助手聊聊' }), { target: { value: '保留侧栏跳转前的目标' } });
    fireEvent.change(screen.getByRole('textbox', { name: '本次工作目录' }), { target: { value: '/work/draft' } });
    fireEvent.change(screen.getByRole('textbox', { name: '完成标准' }), { target: { value: '检查后说明结果' } });
    await user.click(screen.getByRole('checkbox'));
    await user.click(screen.getByRole('button', { name: '打开工作记录' }));
    const rail = screen.getByRole('complementary', { name: 'Agent 工作记录' });
    await user.click(within(rail).getByRole('button', { name: /^发布检查/ }));
    expect(await screen.findByTestId('session-record-id')).toHaveTextContent('session-old');
    await user.click(screen.getByRole('button', { name: '工作台选项' }));
    await user.click(screen.getByRole('menuitem', { name: '返回我的助手' }));
    expect(await screen.findByRole('textbox', { name: '和我的助手聊聊' })).toHaveValue('保留侧栏跳转前的目标');
    expect(screen.getByRole('textbox', { name: '本次工作目录' })).toHaveValue('/work/draft');
    expect(screen.getByRole('textbox', { name: '完成标准' })).toHaveValue('检查后说明结果');
    expect(screen.getByRole('checkbox')).toBeChecked();
  });
  it('carries unsent home text into the discussion without submitting it', async () => {
    const transport = createTransport();
    const view = renderAgent(transport, { initialRoute: '/agent' });
    await waitFor(() => expect(screen.getByRole('button', { name: /进入对话/ })).toBeEnabled());
    fireEvent.change(screen.getByRole('textbox', { name: '和我的助手聊聊' }), { target: { value: '先保留，不发送' } });
    fireEvent.click(screen.getByRole('button', { name: /进入对话/ }));
    expect(await screen.findByTestId('session-initial-draft')).toHaveTextContent('先保留，不发送');
    view.rerender(agentTree(transport, { initialRoute: '/agent?session=primary' }));
    expect(screen.getByTestId('session-initial-draft')).toHaveTextContent('先保留，不发送');
    expect(screen.getByTestId('session-initial-submission')).toBeEmptyDOMElement();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.prompt')).toBe(false);
  });
  it('preserves first submission and discussion handoff across desktop route acknowledgements', async () => {
    const transport = createTransport();
    const view = renderAgent(transport, { initialRoute: '/agent' });
    await waitFor(() => expect(screen.getByRole('button', { name: /进入对话/ })).toBeEnabled());
    fireEvent.change(screen.getByRole('textbox', { name: '和我的助手聊聊' }), { target: { value: '第一条消息' } });
    fireEvent.click(screen.getByRole('button', { name: '发送给我的助手' }));
    const pending = await screen.findByTestId('session-initial-submission');
    expect(pending).toHaveTextContent('第一条消息');
    const submission = pending.textContent;
    view.rerender(agentTree(transport, { initialRoute: '/agent?session=primary' }));
    expect(await screen.findByTestId('session-initial-submission')).toHaveTextContent(submission!);
    fireEvent.click(screen.getByRole('button', { name: '交给助手做' }));
    expect(await screen.findByRole('textbox', { name: '本次工作目录' })).toBeVisible();
    view.rerender(agentTree(transport, { initialRoute: '/agent' }));
    expect(await screen.findByRole('textbox', { name: '本次工作目录' })).toBeVisible();
    expect(screen.getByRole('textbox', { name: '和我的助手聊聊' })).toHaveValue('检查具体目标');
    fireEvent.change(screen.getByRole('textbox', { name: '本次工作目录' }), { target: { value: '/work/demo' } });
    fireEvent.click(screen.getByRole('checkbox'));
    await waitFor(() => expect(screen.getByRole('button', { name: '授权并开始任务' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '授权并开始任务' }));
    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'agent.primary.tasks.create')).toBe(true));
    expect(transport.requests.find(({ request }) => request.pathId === 'agent.primary.tasks.create')?.request.body).toMatchObject({ sourceSessionId: 'primary', sourceMessageId: 'source-cutoff' });
  });
  it('makes the stable assistant the default entry while keeping the advanced creator explicit', async () => {
    const transport = createTransport();
    renderAgent(transport, { initialRoute: '/agent' });
    expect(await screen.findByRole('heading', { name: '有事，接着聊。' })).toBeVisible();
    expect(screen.getByRole('region', { name: 'Agent 工作台' })).toHaveAttribute('data-compact-work');
    expect(screen.queryByRole('group', { name: 'Agent 界面模式' })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: /进入对话/ })).toBeEnabled());
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.primary.ensure')).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: /新建独立对话或多人协作/ }));
    expect(await screen.findByRole('heading', { name: '今天想完成什么？' })).toBeVisible();
    expect(screen.getByRole('region', { name: 'Agent 工作台' })).not.toHaveAttribute('data-compact-work');
    expect(transport.requests.some(({ request }) => ['agent.sessions.create', 'agent.session.prompt'].includes(request.pathId))).toBe(false);
  });
  it('uses the owned catalog capability and preserves a Durable draft when a replacement Host withdraws support', async () => {
    const user = userEvent.setup();
    const available = createTransport({ modelCatalog: { providers: [], selected: {}, sessionEngines: { durable: { available: true } } } });
    const view = renderAgent(available);
    await user.click(screen.getByRole('button', { name: /^权限 ·/ }));
    await waitFor(() => expect(screen.getByRole('menuitemradio', { name: 'Pi Durable（实验）' })).not.toHaveAttribute('aria-disabled', 'true'));
    await user.click(screen.getByRole('menuitemradio', { name: 'Pi Durable（实验）' }));
    await user.type(screen.getByRole('textbox', { name: '描述你想完成的工作' }), '继续已有目标');
    const unavailable = createTransport();
    view.rerender(agentTree(unavailable));
    await user.click(screen.getByRole('button', { name: /^权限 ·/ }));
    await waitFor(() => expect(screen.getByRole('menuitemradio', { name: 'Pi Durable（实验）' })).toHaveAttribute('aria-disabled', 'true'));
    expect(screen.getByRole('menuitemradio', { name: 'Pi Durable（实验）' })).toHaveAttribute('aria-checked', 'true');
    await user.keyboard('{Escape}');
    await user.click(screen.getByRole('button', { name: '开始 Session' }));
    expect(await screen.findByText(/当前 Pi Runtime 暂不支持 Durable/)).toBeVisible();
    expect(screen.getByRole('textbox', { name: '描述你想完成的工作' })).toHaveValue('继续已有目标');
    expect(unavailable.requests.some(({ request }) => request.pathId === 'agent.sessions.create' || request.pathId === 'agent.session.prompt')).toBe(false);
  });

  it('warms an exact rail record on intent without selecting it or sending work', async () => {
    const loader = await import('./agent-workspace-loader');
    const warm = vi.spyOn(loader, 'warmAgentWorkspace');
    try {
      const transport = createTransport();
      renderAgent(transport);
      fireEvent.click(await screen.findByRole('button', { name: '打开工作记录' }));
      const rail = screen.getByRole('complementary', { name: 'Agent 工作记录' });
      const target = await within(rail).findByRole('button', { name: /^迁移作战室/ });
      warm.mockClear();
      fireEvent.pointerEnter(target);
      fireEvent.focus(target);
      fireEvent.pointerDown(target);
      expect(warm.mock.calls).toEqual([['room'], ['room'], ['room']]);
      expect(screen.queryByText('Room 工作区 · room-old')).not.toBeInTheDocument();
      expect(transport.requests.some(({ request }) => ['agent.room.message', 'agent.session.prompt', 'agent.jev.command'].includes(request.pathId))).toBe(false);
    } finally { warm.mockRestore(); }
  });

  it('announces a selected workspace while it loads and retains its draft when ready', async () => {
    const ready = deferred<void>();
    workspaceLoading.room = ready.promise;
    try {
      renderAgent(createTransport(), { initialRoute: '/agent?room=room-old&draft=loading%20draft' });
      const loading = await screen.findByText('正在打开 Room 工作区…');
      expect(loading.closest('[role="status"]')).toBeInTheDocument();
      expect(screen.queryByText('Room 工作区 · room-old')).not.toBeInTheDocument();
      await act(async () => { workspaceLoading.room = null; ready.resolve(); });
      expect(await screen.findByText('Room 工作区 · room-old')).toBeInTheDocument();
      expect(screen.getByTestId('room-initial-draft')).toHaveTextContent('loading draft');
      expect(screen.queryByText('正在打开 Room 工作区…')).not.toBeInTheDocument();
    } finally { workspaceLoading.room = null; ready.resolve(); }
  });

  it('opens a selected Room without starting the hidden Session catalog', async () => {
    const sessions = deferred<unknown>();
    const transport = createTransport({ sessionCatalogHandler: () => sessions.promise });
    renderAgent(transport, { initialRoute: '/agent?room=room-old' });

    expect(await screen.findByText('Room 工作区 · room-old')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('room-record-title')).toHaveTextContent('迁移作战室'));
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.sessions.list')).toBe(false);
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.roles.list')).toBe(true);
  });

  it('shows Room directory results while a visible multi-page Session catalog is slow', async () => {
    const laterPage = deferred<unknown>();
    const transport = createTransport({ sessionCatalogHandler: (request) => (
      request.query?.beforeId
        ? laterPage.promise
        : { items: [{ id: 'session-old', title: '发布检查', mode: 'coordinator', status: 'idle', updatedAtMs: 2 }], hasMore: true, nextBeforeUpdatedAtMs: 2, nextBeforeId: 'session-old' }
    ) });
    renderAgent(transport);

    await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.list')).toHaveLength(2));
    const rail = screen.getByRole('complementary', { name: 'Agent 工作记录' });
    expect(await within(rail).findByRole('button', { name: /^迁移作战室/ })).toBeInTheDocument();
    expect(rail.querySelector('.paw-agent-recents')).toHaveAttribute('aria-busy', 'true');
  });

  it('shows the first Session page while later pages and model metadata are pending', async () => {
    const laterPage = deferred<unknown>();
    const metadata = deferred<unknown>();
    const transport = createTransport({
      modelCatalog: () => metadata.promise,
      sessionCatalogHandler: (request) => request.query?.beforeId
        ? laterPage.promise
        : { items: [{ id: 'session-first', title: '首屏工作记录', mode: 'coordinator', status: 'idle', updatedAtMs: 2 }], hasMore: true, nextBeforeUpdatedAtMs: 2, nextBeforeId: 'session-first' },
    });
    renderAgent(transport);
    const rail = screen.getByRole('complementary', { name: 'Agent 工作记录' });
    expect(await within(rail).findByRole('button', { name: /^首屏工作记录/ })).toBeInTheDocument();
    expect(rail.querySelector('.paw-agent-recents')).toHaveAttribute('aria-busy', 'true');
    await act(async () => {
      laterPage.resolve({ items: [{ id: 'session-older', title: '更早工作记录', mode: 'coordinator', status: 'idle', updatedAtMs: 1 }], hasMore: false });
      await laterPage.promise;
    });
    expect(await within(rail).findByRole('button', { name: /^更早工作记录/ })).toBeInTheDocument();
    expect(within(rail).getByRole('button', { name: /^首屏工作记录/ })).toBeInTheDocument();
    await act(async () => {
      metadata.resolve({ providers: [], selected: {} });
      await metadata.promise;
    });
    await waitFor(() => expect(rail.querySelector('.paw-agent-recents')).not.toHaveAttribute('aria-busy'));
  });

  it('makes the model usable while the work history is still pending', async () => {
    const history = deferred<unknown>();
    renderAgent(createTransport({ sessionCatalogHandler: () => history.promise, modelCatalog: modelCatalog() }));
    const picker = await screen.findByRole('button', { name: /^模型与推理：GPT-5.6 Luna/ });
    await waitFor(() => expect(picker).not.toHaveAttribute('aria-busy'));
    expect(screen.getByText('正在读取工作记录…')).toBeInTheDocument();
    await act(async () => {
      history.resolve({ ok: true, items: [] });
      await history.promise;
    });
  });

  it('shows model failure and recovery without waiting for pending history', async () => {
    const history = deferred<unknown>();
    renderAgent(createTransport({ sessionCatalogHandler: () => history.promise,
      modelCatalog: () => Promise.reject(new Error('model directory unavailable')) }));
    expect(await screen.findByText('模型目录暂时无法读取。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '重新读取模型' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^模型与推理：/ })).not.toHaveAttribute('aria-busy');
    await act(async () => {
      history.resolve({ ok: true, items: [] });
      await history.promise;
    });
  });

  it('retains a published first page and reports a failed later page', async () => {
    const transport = createTransport({
      sessionCatalogHandler: (request) => request.query?.beforeId
        ? Promise.reject(new Error('older catalog page unavailable'))
        : { items: [{ id: 'session-first', title: '已读取工作记录', mode: 'coordinator', status: 'idle', updatedAtMs: 2 }], hasMore: true, nextBeforeUpdatedAtMs: 2, nextBeforeId: 'session-first' },
    });
    renderAgent(transport);
    const rail = screen.getByRole('complementary', { name: 'Agent 工作记录' });
    expect(await within(rail).findByRole('button', { name: /^已读取工作记录/ })).toBeInTheDocument();
    expect(await within(rail).findByText('部分 Agent 目录暂时不可用。')).toBeInTheDocument();
    await waitFor(() => expect(rail.querySelector('.paw-agent-recents')).not.toHaveAttribute('aria-busy'));
  });

  it('aborts an abandoned Session page when switching to a closed Room directory', async () => {
    const firstPage = deferred<unknown>();
    const transport = createTransport({ sessionCatalogHandler: () => firstPage.promise });
    const view = renderAgent(transport);
    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'agent.sessions.list')).toBe(true));
    const pending = transport.requests.find(({ request }) => request.pathId === 'agent.sessions.list')!.request;

    view.rerender(agentTree(transport, { initialRoute: '/agent?room=room-old' }));
    expect(await screen.findByText('Room 工作区 · room-old')).toBeInTheDocument();
    expect(pending.signal?.aborted).toBe(true);
    await act(async () => {
      firstPage.resolve({ items: [], hasMore: true, nextBeforeUpdatedAtMs: 42, nextBeforeId: 'oldest-page' });
      await firstPage.promise;
    });
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.list')).toHaveLength(1);
  });

  it('loads the complete Session directory when the selected Room rail opens', async () => {
    const user = userEvent.setup();
    const transport = createTransport();
    renderAgent(transport, { initialRoute: '/agent?room=room-old' });
    expect(await screen.findByText('Room 工作区 · room-old')).toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.sessions.list')).toBe(false);

    await user.click(screen.getByRole('button', { name: '打开工作记录' }));
    const rail = screen.getByRole('complementary', { name: 'Agent 工作记录' });
    expect(await within(rail).findByRole('button', { name: /^发布检查/ })).toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.sessions.list')).toBe(true);
  });

  it('commits the new-work shell before catalog hydration starts on the next frame', async () => {
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      frames.push(callback);
      return frames.length;
    });
    vi.stubGlobal('cancelAnimationFrame', (handle: number) => {
      frames[handle - 1] = () => undefined;
    });
    try {
      const transport = createTransport();
      renderAgent(transport);

      // The first interactive surface is already stable, but the four catalog
      // requests are not allowed to consume the Dock click's paint frame.
      expect(screen.getByRole('heading', { name: '今天想完成什么？' })).toBeInTheDocument();
      expect(catalogRequestPaths(transport)).toEqual([]);
      expect(frames).toHaveLength(1);

      await act(async () => {
        frames.splice(0).forEach((frame) => frame(performance.now()));
        await Promise.resolve();
      });

      await waitFor(() => expect(catalogRequestPaths(transport)).toEqual([
        'agent.sessions.list',
        'agent.rooms.list',
        'agent.roles.list',
        'agent.role.models',
      ]));
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('cancels a scheduled catalog hydration on unmount instead of starting a duplicate request', () => {
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      frames.push(callback);
      return frames.length;
    });
    vi.stubGlobal('cancelAnimationFrame', (handle: number) => {
      frames[handle - 1] = () => undefined;
    });
    try {
      const transport = createTransport();
      const view = renderAgent(transport);
      expect(frames).toHaveLength(1);

      view.unmount();
      act(() => frames.splice(0).forEach((frame) => frame(performance.now())));

      expect(catalogRequestPaths(transport)).toEqual([]);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('hydrates a restored Room from its live snapshot while an unfocused window defers the directory', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    render(
      <PawOsAppSurfaceProvider active={false} appId="agent" height={720} width={1_080} windowId="agent-room">
        {agentTree(transport, { initialRoute: '/agent?room=room-restored' })}
      </PawOsAppSurfaceProvider>,
    );

    expect(await screen.findByTestId('room-record-title')).toHaveTextContent('missing');
    expect(catalogRequestPaths(transport)).toEqual([]);
    await user.click(screen.getByRole('button', { name: '接收 Room 快照' }));

    expect(screen.getByTestId('room-record-title')).toHaveTextContent('恢复的协作主对话');
    expect(catalogRequestPaths(transport)).toEqual([]);
  });

  it('uses one rail for Sessions and Rooms and starts on the central new-work composer', async () => {
    renderAgent();

    expect(await screen.findByRole('heading', { name: '今天想完成什么？' })).toBeInTheDocument();
    const rail = screen.getByRole('complementary', { name: 'Agent 工作记录' });
    expect(within(rail).getByText('工作记录')).toBeInTheDocument();
    expect(await within(rail).findByRole('button', { name: /^发布检查/ })).toBeInTheDocument();
    expect(within(rail).getByRole('button', { name: /迁移作战室/ })).toBeInTheDocument();
    expect(within(rail).getByText('paw')).toBeInTheDocument();
    expect(within(rail).getByText('Session')).toBeInTheDocument();
    expect(within(rail).getByText('Room')).toBeInTheDocument();
    expect(rail.querySelector('[data-paw-app-icon]')).not.toBeInTheDocument();
    expect(screen.getByRole('radio', { name: 'Session' })).toBeChecked();
    expect(screen.getByRole('radio', { name: 'Room' })).not.toBeChecked();
  });

  it('keeps unbound conversations in an explicit folder instead of guessing from titles', async () => {
    renderAgent(createTransport({
      sessions: [{
        id: 'session-unbound',
        title: '迁移作战室',
        mode: 'coordinator',
        status: 'idle',
        updatedAtMs: 4,
        workspaceRoots: [],
        messageCount: 1,
        lastMessagePreview: '',
      }],
    }));

    const rail = screen.getByRole('complementary', { name: 'Agent 工作记录' });
    expect(await within(rail).findByText('未绑定项目')).toBeInTheDocument();
    expect(within(rail).getAllByRole('button', { name: /^迁移作战室/ })).toHaveLength(2);
    expect(within(rail).getByText('paw')).toBeInTheDocument();
  });

  it('does not offer the system root as a project-scoped workspace', async () => {
    const user = userEvent.setup();
    renderAgent(createTransport({
      sessions: [
        {
          id: 'session-system-wide', title: '全系统会话', mode: 'coordinator', status: 'idle',
          updatedAtMs: 4, workspaceRoots: ['/'], messageCount: 1, lastMessagePreview: '',
        },
        {
          id: 'session-project', title: '项目会话', mode: 'coordinator', status: 'idle',
          updatedAtMs: 3, workspaceRoots: ['/work/paw'], messageCount: 1, lastMessagePreview: '',
        },
      ],
      rooms: [],
    }));

    expect(await screen.findByRole('button', { name: '起始项目 · work/paw' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '起始项目 · /' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /起始项目/ }));
    const menu = screen.getByRole('menu');
    expect(within(menu).getByRole('menuitemradio', { name: /paw/ })).toBeInTheDocument();
    expect(within(menu).queryByRole('menuitemradio', { name: /^\/$/ })).not.toBeInTheDocument();
  });

  it('opens a directly targeted Room planet as a full Session without listing it as an ordinary conversation file', async () => {
    renderAgent(createTransport({
      sessions: [{
        id: 'session-earth',
        title: 'Room 内部会话',
        mode: 'coordinator',
        status: 'busy',
        updatedAtMs: 4,
        workspaceRoots: ['/work/paw'],
        messageCount: 4,
        lastMessagePreview: '正在汇总',
        roomParticipant: true,
      }],
    }), {
      target: { kind: 'session', id: 'session-earth', title: 'Earth' },
    });

    expect(await screen.findByText('Session 工作区')).toBeInTheDocument();
    const rail = screen.getByRole('complementary', { name: 'Agent 工作记录' });
    await waitFor(() => expect(within(rail).queryByText('Room 内部会话')).not.toBeInTheDocument());
  });

  it('reconciles canonical selected-Session metadata while the rail is closed', async () => {
    const sessionId = 'eval-session';
    const transport = createTransport({
      sessions: [{
        id: sessionId,
        title: '评测快照',
        mode: 'coordinator',
        status: 'idle',
        updatedAtMs: 4,
        workspaceRoots: [],
        messageCount: 1,
        lastMessagePreview: '真实评测记录',
        evaluationSnapshot: true,
      }],
    });

    renderAgent(transport, { initialRoute: `/agent?session=${sessionId}` });

    expect(screen.getByTestId('session-record-id')).toHaveTextContent(sessionId);
    expect(screen.getByTestId('session-record-known')).toHaveTextContent('false');
    await waitFor(() => expect(screen.getByTestId('session-record-read-only')).toHaveTextContent('true'));
    expect(screen.getByTestId('session-record-known')).toHaveTextContent('true');
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.list')).toHaveLength(1);
  });

  it('mounts a directly targeted Session before loading its optional work-record catalog', async () => {
    const catalog = deferred<unknown>();
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': () => catalog.promise,
      'agent.rooms.list': () => catalog.promise,
      'agent.roles.list': () => catalog.promise,
      'agent.role.models': () => catalog.promise,
    } });

    const user = userEvent.setup();
    renderAgent(transport, { initialRoute: '/agent?session=session-latency' });

    expect(screen.getByTestId('session-record-id')).toHaveTextContent(/^session-latency$/);
    await waitFor(() => expect(catalogRequestPaths(transport)).toEqual(['agent.sessions.list']));

    await user.click(screen.getByRole('button', { name: '打开工作记录' }));
    await waitFor(() => expect(catalogRequestPaths(transport)).toEqual([
      'agent.sessions.list',
      'agent.sessions.list',
      'agent.rooms.list',
      'agent.roles.list',
    ]));
  });

  it('projects truthful bounded status into virtual conversation files and lets project folders fold in place', async () => {
    const now = Date.now();
    const user = userEvent.setup();
    renderAgent(createTransport({
      rooms: [roomFixture({
        updatedAtMs: now,
        workItems: [
          workItemFixture({ id: 'done', state: 'done', resultSummary: '旧结果已生成', updatedAtMs: now }),
          workItemFixture({ id: 'blocked', state: 'blocked', blocker: { reason: '等待沙盒授权' }, updatedAtMs: now - 10 }),
        ],
      })],
      sessions: [
        {
          id: 'session-busy', title: 'Trace 地基', mode: 'coordinator', status: 'busy', updatedAtMs: now,
          workspaceRoots: ['/work/paw'], messageCount: 3, lastMessagePreview: '正在建立\nTrace 关联',
        },
        {
          id: 'session-faulted', title: 'RAG 检查', mode: 'coordinator', status: 'faulted', updatedAtMs: now - 1,
          workspaceRoots: ['/work/paw'], messageCount: 2, lastMessagePreview: '',
        },
      ],
    }));

    const rail = screen.getByRole('complementary', { name: 'Agent 工作记录' });
    expect(await within(rail).findByText('当前公开内容：正在建立 Trace 关联')).toBeInTheDocument();
    expect(within(rail).getByText('故障原因不可用')).toBeInTheDocument();
    expect(within(rail).getByText('阻塞：等待沙盒授权')).toBeInTheDocument();
    expect(within(rail).queryByText('最近结果：旧结果已生成')).not.toBeInTheDocument();

    await user.click(within(rail).getByText('paw'));
    await waitFor(() => expect(within(rail).queryByText('Trace 地基')).not.toBeInTheDocument());
    await user.click(within(rail).getByText('paw'));
    expect(await within(rail).findByText('Trace 地基')).toBeInTheDocument();
  });

  it('labels delivered WorkItems without claiming a Runtime terminal before Room hydration', async () => {
    renderAgent(createTransport({
      rooms: [roomFixture({
        workItems: [
          workItemFixture({
            state: 'done',
            resultSummary: '最终 Root 结果已提交',
            completedAtMs: Date.now(),
          }),
        ],
      })],
      sessions: [],
    }));

    const rail = screen.getByRole('complementary', { name: 'Agent 工作记录' });
    const roomRow = await within(rail).findByRole('button', { name: /迁移作战室/ });
    expect(within(roomRow).getByText(/^已有交付 · 2 位伙伴/)).toBeInTheDocument();
    expect(within(roomRow).queryByText(/^进行中 ·/)).not.toBeInTheDocument();
  });

  it('does not infer a live execution from an unfinished WorkItem in the directory', async () => {
    renderAgent(createTransport({ rooms: [roomFixture({ workItems: [workItemFixture({ state: 'active' })] })], sessions: [] }));
    const rail = screen.getByRole('complementary', { name: 'Agent 工作记录' });
    const row = await within(rail).findByRole('button', { name: /迁移作战室/ });
    expect(within(row).getByText(/^有待办 ·/)).toBeInTheDocument();
    expect(row.closest('[data-work-state]')).toHaveAttribute('data-work-state', 'neutral');
    expect(row).not.toHaveTextContent('进行中');
  });

  it('updates the Room list from the same terminal projection as the open workspace', async () => {
    const room = roomFixture({ workItems: [workItemFixture({ state: 'done', resultSummary: '已交付的成果' })] });
    renderAgent(createTransport({ rooms: [room], sessions: [] }));
    const rail = screen.getByRole('complementary', { name: 'Agent 工作记录' });
    const row = await within(rail).findByRole('button', { name: /迁移作战室/ });
    act(() => useRoomLiveStore.getState().applyEvents(room.id, [parseRoomEvent({
      schemaVersion: 'rag-ime.agent-room-event.v1', roomId: room.id,
      eventId: `${room.id}:1`, sequence: 1, resumeToken: `${room.id}:1`,
      turnId: 'cancelled-root', eventType: 'participant_status',
      participantId: null, sourceSessionId: '', createdAtMs: 10,
      payload: { status: 'cancellation_applied', rootId: 'cancelled-root', cancellationReceiptId: 'cancel-1', pendingTargets: [] },
    })]));
    expect(within(row).getByText(/^本轮已停止 ·/)).toBeInTheDocument();
    expect(within(row).getByText('最近结果：已交付的成果')).toBeInTheDocument();
  });

  it('exposes the rail relationship and returns focus when Escape closes it', async () => {
    const user = userEvent.setup();
    renderAgent();

    const toggle = await screen.findByRole('button', { name: '打开工作记录' });
    const rail = screen.getByRole('complementary', { name: 'Agent 工作记录' });
    expect(rail).toHaveAttribute('id', 'paw-agent-work-records');
    expect(toggle).toHaveAttribute('aria-controls', 'paw-agent-work-records');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');

    await user.click(toggle);
    expect(screen.getByRole('button', { name: '收起工作记录' })).toHaveAttribute('aria-expanded', 'true');
    await waitFor(() => expect(screen.getByRole('textbox', { name: '搜索 Session 与 Room' })).toHaveFocus());
    expect(document.querySelector('.paw-agent-stage')).toHaveAttribute('inert');
    expect(rail).not.toHaveAttribute('inert');
    await user.keyboard('{Escape}');

    expect(screen.getByRole('button', { name: '打开工作记录' })).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByRole('button', { name: '打开工作记录' })).toHaveFocus();
    expect(document.querySelector('.paw-agent-stage')).not.toHaveAttribute('inert');
    expect(rail).toHaveAttribute('inert');
  });

  it('restores focus and the work surface when starting from the history drawer', async () => {
    const user = userEvent.setup();
    renderAgent();
    await user.click(await screen.findByRole('button', { name: '打开工作记录' }));
    const rail = screen.getByRole('complementary', { name: 'Agent 工作记录' });
    await user.click(within(rail).getByRole('button', { name: '新建工作' }));
    expect(screen.getByRole('button', { name: '打开工作记录' })).toHaveFocus();
    expect(document.querySelector('.paw-agent-stage')).not.toHaveAttribute('inert');
    expect(screen.getByRole('heading', { name: '今天想完成什么？' })).toBeInTheDocument();
  });

  it('returns focus to the owning chip when Escape closes an anchored composer menu', async () => {
    const user = userEvent.setup();
    renderAgent();

    const chip = await screen.findByRole('button', { name: /完全访问/ });
    await user.click(chip);
    expect(screen.getByRole('menu')).toBeInTheDocument();
    await user.tab();
    expect(chip).not.toHaveFocus();
    await user.keyboard('{Escape}');

    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(chip).toHaveFocus();
  });

  it('keeps the containing window identity aligned when the same Agent window moves between Room and Session', async () => {
    const bindAgentMain = vi.fn();
    const transport = createTransport();
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ControlTransportProvider transport={transport}>
          <TooltipProvider>
            <PawOsDesktopProvider bindAgentMain={bindAgentMain} openWindow={() => undefined}>
              <PawOsAppSurfaceProvider appId="agent" height={720} width={1_080} windowId="agent">
                <PawAgentApp initialRoute="/agent?new=advanced" />
              </PawOsAppSurfaceProvider>
            </PawOsDesktopProvider>
          </TooltipProvider>
        </ControlTransportProvider>
      </QueryClientProvider>,
    );

    await screen.findByRole('heading', { name: '今天想完成什么？' });
    await waitFor(() => expect(bindAgentMain).toHaveBeenCalledWith('agent'));
    const rail = screen.getByRole('complementary', { name: 'Agent 工作记录' });

    await user.click(await within(rail).findByRole('button', { name: /^迁移作战室/ }));
    await waitFor(() => expect(bindAgentMain).toHaveBeenCalledWith('agent', expect.objectContaining({
      kind: 'room', id: 'room-old', title: '迁移作战室',
    })));

    await user.click(within(rail).getByRole('button', { name: /^发布检查/ }));
    await waitFor(() => expect(bindAgentMain).toHaveBeenLastCalledWith('agent', {
      kind: 'session', id: 'session-old', title: '发布检查',
    }));
  });

  it('hydrates untouched composer defaults from the production configuration authority', async () => {
    const catalog = deferred<unknown>();
    const transport = createTransport({
      modelCatalog: catalog.promise,
      preferencesHandler: () => preferenceSettings({
        modelReference: 'gpt/gpt-5.6-sol',
        thinkingLevel: 'max',
        executionMode: 'read_only',
      }),
    });
    renderAgent(transport);
    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'agent.role.models')).toBe(true));
    await act(async () => { catalog.resolve(modelCatalog()); await catalog.promise; });

    expect(await screen.findByRole('button', { name: '模型与推理：GPT-5.6 Sol · gpt · 最高' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /只读/ })).toBeInTheDocument();
  });

  it('does not let late authority hydration overwrite a composer choice already made by the user', async () => {
    const settings = deferred<unknown>();
    const catalog = deferred<unknown>();
    const user = userEvent.setup();
    const transport = createTransport({ modelCatalog: catalog.promise, preferencesHandler: () => settings.promise });
    renderAgent(transport);
    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'agent.role.models')).toBe(true));
    await act(async () => { catalog.resolve(modelCatalog()); await catalog.promise; });

    await user.click(await screen.findByRole('button', { name: /GPT-5\.6 Luna/ }));
    const picker = screen.getByRole('dialog', { name: '选择模型与推理强度' });
    await user.click(within(picker).getByRole('button', { name: /更换模型/ }));
    await user.click(within(picker).getByRole('option', { name: '选择模型 GPT-5.6 Sol' }));
    await act(async () => {
      settings.resolve(preferenceSettings({
        modelReference: 'gpt/gpt-5.6-terra',
        thinkingLevel: 'high',
        executionMode: 'read_only',
      }));
      await settings.promise;
    });

    await waitFor(() => expect(screen.getByRole('button', { name: /只读/ })).toBeInTheDocument());
    expect(screen.getByRole('button', { name: '模型与推理：GPT-5.6 Sol · gpt · 高' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /模型与推理：GPT-5\.6 Terra/ })).not.toBeInTheDocument();
  });

  it('shows an explicit configuration recovery action when composer defaults cannot be read', async () => {
    renderAgent(createTransport({ preferencesHandler: () => { throw new Error('配置服务暂时不可用'); } }));

    expect(await screen.findByRole('alert')).toHaveTextContent('配置服务暂时不可用');
    expect(screen.getByRole('button', { name: '重新读取' })).toBeInTheDocument();
  });

  it('reports real model-catalog facts in the picker without a runtime connectivity claim', async () => {
    const user = userEvent.setup();
    const empty = renderAgent();
    await screen.findByRole('heading', { name: '今天想完成什么？' });
    expect(screen.queryByText(/Pi Runtime/)).not.toBeInTheDocument();
    expect(screen.queryByText(/个可用模型/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /模型与推理/ })).toBeDisabled();
    empty.unmount();

    renderAgent(createTransport({ modelCatalog: modelCatalog() }));
    const model = await screen.findByRole('button', { name: '模型与推理：GPT-5.6 Luna · gpt · 高' });
    expect(model).toBeEnabled();
    await user.click(model);
    const picker = screen.getByRole('dialog', { name: '选择模型与推理强度' });
    await user.click(within(picker).getByRole('button', { name: /更换模型/ }));
    expect(within(picker).getAllByRole('option')).toHaveLength(3);
    expect(within(picker).getByRole('option', { name: '选择模型 GPT-5.6 Luna' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByText(/Pi Runtime/)).not.toBeInTheDocument();
  });

  it('offers a Trace Agent handoff when the Agent directory operation fails', async () => {
    const user = userEvent.setup();
    const transport = createTransport({
      archiveHandler: () => { throw new Error('archive unavailable'); },
    });
    renderAgent(transport);
    await user.click(screen.getByRole('button', { name: '打开工作记录' }));
    const rail = screen.getByRole('complementary', { name: 'Agent 工作记录' });
    await user.click(await within(rail).findByRole('button', { name: '更多“发布检查”操作' }));
    await user.click(await screen.findByRole('menuitem', { name: '归档 Session' }));
    await waitFor(() => expect(within(rail).getByText('archive unavailable')).toBeInTheDocument());

    window.location.hash = '';
    await user.click(screen.getByRole('button', { name: '交给 Trace Agent' }));
    const handoff = parseTraceAgentHandoff(window.location.hash.replace(/^#\/trace-agent\?/u, ''));
    expect(handoff).toMatchObject({
      kind: 'session',
      entityId: 'session-old',
      sessionId: 'session-old',
      sourceRoute: '/agent?session=session-old',
      refs: { operation: 'archive', surface: 'agent-directory' },
    });
  });

  it('reflects the selected permission risk on the composer chip mark', async () => {
    const user = userEvent.setup();
    renderAgent();

    // The chip signals the mode through the PermissionMark glyph family, not
    // a colour dot: the mark's data-mark kind must follow the selection.
    const chip = await screen.findByRole('button', { name: /完全访问/ });
    expect(chip.querySelector('[data-mark="permission-per-action"]')).toBeInTheDocument();

    await user.click(chip);
    await user.click(await screen.findByRole('menuitemradio', { name: /^全自动/ }));
    expect(screen.getByRole('button', { name: /全自动/ }).querySelector('[data-mark="permission-full-trust"]'))
      .toBeInTheDocument();
  });

  it('orders 继续工作 by real recency instead of catalog list position', async () => {
    renderAgent(createTransport({
      sessions: [
        { id: 's-a', title: '最旧的检查', mode: 'coordinator', status: 'idle', updatedAtMs: 1, workspaceRoots: ['/work/paw'], messageCount: 1, lastMessagePreview: '' },
        { id: 's-b', title: '较旧的检查', mode: 'coordinator', status: 'idle', updatedAtMs: 2, workspaceRoots: ['/work/paw'], messageCount: 1, lastMessagePreview: '' },
        { id: 's-c', title: '次新的检查', mode: 'coordinator', status: 'idle', updatedAtMs: 4, workspaceRoots: ['/work/paw'], messageCount: 1, lastMessagePreview: '' },
        { id: 's-d', title: '最新的检查', mode: 'coordinator', status: 'idle', updatedAtMs: 9, workspaceRoots: ['/work/paw'], messageCount: 1, lastMessagePreview: '' },
      ],
    }));

    const recent = (await screen.findByRole('heading', { name: '继续工作' })).parentElement!;
    expect(within(recent).getByRole('button', { name: /最新的检查/ })).toBeInTheDocument();
    expect(within(recent).queryByRole('button', { name: /最旧的检查/ })).not.toBeInTheDocument();
  });

  it('creates a Session and sends the first prompt from the same composer', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    renderAgent(transport);

    const composer = await screen.findByRole('textbox', { name: '描述你想完成的工作' });
    await user.type(composer, '检查发布门禁并给出结论');
    await user.click(screen.getByRole('button', { name: '开始 Session' }));

    await waitFor(() => expect(transport.requests.map(({ request }) => request.pathId)).toEqual(
      expect.arrayContaining(['agent.sessions.create', 'agent.session.prompt']),
    ));
    const prompt = transport.requests.find(({ request }) => request.pathId === 'agent.session.prompt')?.request;
    expect(prompt?.params).toEqual({ sessionId: 'session-new' });
    expect(prompt?.body).toMatchObject({ message: '检查发布门禁并给出结论' });
    expect(await screen.findByText('Session 工作区')).toBeInTheDocument();
  });

  it('opens the Session before a slow first-prompt receipt settles', async () => {
    const prompt = deferred<unknown>();
    const transport = createTransport({ promptHandler: () => prompt.promise });
    const user = userEvent.setup();
    renderAgent(transport);

    await user.type(await screen.findByRole('textbox', { name: '描述你想完成的工作' }), '立即进入工作现场');
    await user.click(screen.getByRole('button', { name: '开始 Session' }));

    await waitFor(() => expect(screen.getByText('Session 工作区')).toBeInTheDocument());
    expect(transport.requests.map(({ request }) => request.pathId)).toContain('agent.session.prompt');
    prompt.resolve({ ok: true });
  });

  it('admits the first prompt without waiting for model configuration', async () => {
    const modelSelect = deferred<unknown>();
    const transport = createTransport({
      modelHandler: () => modelSelect.promise,
      modelCatalog: {
        providers: [{ id: 'gpt', models: [{ id: 'gpt-5.6-luna', name: 'GPT-5.6 Luna', thinkingLevels: ['off'] }] }],
        selected: { provider: 'gpt', id: 'gpt-5.6-luna' },
      },
    });
    const user = userEvent.setup();
    renderAgent(transport);

    await user.type(await screen.findByRole('textbox', { name: '描述你想完成的工作' }), '不要等待模型配置');
    await user.click(screen.getByRole('button', { name: '开始 Session' }));

    await waitFor(() => expect(screen.getByText('Session 工作区')).toBeInTheDocument());
    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.prompt')).toBe(true));
    modelSelect.resolve({ ok: true });
  });

  it('keeps the optimistic Session visible when a catalog refresh is stale', async () => {
    const transport = createTransport({ staleCatalogAfterCreate: true });
    const user = userEvent.setup();
    renderAgent(transport);

    await user.type(await screen.findByRole('textbox', { name: '描述你想完成的工作' }), '保持目录一致');
    await user.click(screen.getByRole('button', { name: '开始 Session' }));

    const rail = screen.getByRole('complementary', { name: 'Agent 工作记录' });
    await waitFor(() => expect(within(rail).getByText('保持目录一致')).toBeInTheDocument());
  });

  it('hydrates a new-work draft from the PAWOS route and reacts to a later deep link', async () => {
    const transport = createTransport();
    const view = renderAgent(transport, { initialRoute: '/agent?draft=先检查发布门禁' });

    expect(await screen.findByRole('textbox', { name: '和我的助手聊聊' })).toHaveValue('先检查发布门禁');

    view.rerender(agentTree(transport, { initialRoute: '/agent?draft=再检查安装状态' }));
    expect(await screen.findByRole('textbox', { name: '和我的助手聊聊' })).toHaveValue('再检查安装状态');
  });

  it('keeps a Room deep-link draft so a satellite can return input to the shared composer', async () => {
    renderAgent(createTransport(), { initialRoute: '/agent?room=room-old&draft=%40Mars%20' });

    expect(await screen.findByText('Room 工作区 · room-old')).toBeInTheDocument();
    expect(screen.getByTestId('room-initial-draft').textContent).toBe('@Mars ');
  });

  it('accepts the sessionId compatibility deep link without returning to new work', async () => {
    renderAgent(createTransport(), { initialRoute: '/agent?sessionId=session-old' });

    expect(await screen.findByText('Session 工作区')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '交给 Agent 一件事。' })).not.toBeInTheDocument();
  });

  it('archives, reveals, restores, and deletes Sessions through the real lifecycle routes', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    renderAgent(transport);
    await user.click(screen.getByRole('button', { name: '打开工作记录' }));
    const rail = screen.getByRole('complementary', { name: 'Agent 工作记录' });

    await user.click(await within(rail).findByRole('button', { name: '更多“发布检查”操作' }));
    await user.click(await screen.findByRole('menuitem', { name: '归档 Session' }));
    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.archive')).toBe(true));
    expect(screen.queryByRole('button', { name: /^发布检查/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '工作记录选项' }));
    await user.click(await screen.findByRole('menuitemcheckbox', { name: '显示已归档 Session' }));
    await waitFor(() => expect(rail.querySelector('.paw-agent-recents')).not.toHaveAttribute('aria-busy', 'true'));
    await user.click(await screen.findByRole('button', { name: '更多“发布检查”操作' }));
    await user.click(await screen.findByRole('menuitem', { name: '恢复 Session' }));

    await user.click(await screen.findByRole('button', { name: '更多“发布检查”操作' }));
    await user.click(await screen.findByRole('menuitem', { name: '删除 Session' }));
    const dialog = await screen.findByRole('dialog', { name: /删除“发布检查”/ });
    await user.click(within(dialog).getByRole('button', { name: '删除' }));

    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.delete')).toBe(true));
    expect(screen.queryByRole('button', { name: /^发布检查/ })).not.toBeInTheDocument();
  });

  it('does not let a late archive receipt for A replace the newer active Session B', async () => {
    const user = userEvent.setup();
    const archived = deferred<unknown>();
    const sessions = [
      { id: 'session-a', title: '任务 A', mode: 'assistant', status: 'idle', updatedAtMs: 2, workspaceRoots: ['/work/paw'], messageCount: 1, lastMessagePreview: '' },
      { id: 'session-b', title: '任务 B', mode: 'assistant', status: 'idle', updatedAtMs: 1, workspaceRoots: ['/work/paw'], messageCount: 1, lastMessagePreview: '' },
    ];
    const transport = createTransport({ sessions, archiveHandler: () => archived.promise });
    renderAgent(transport, { initialRoute: '/agent?session=session-a' });
    await screen.findByTestId('session-record-id');
    await user.click(screen.getByRole('button', { name: '打开工作记录' }));
    await waitFor(() => {
      expect(document.querySelector('.paw-agent-recents')).toBeInTheDocument();
      expect(document.querySelector('.paw-agent-recents')).not.toHaveAttribute('aria-busy', 'true');
    });
    await user.click(screen.getByRole('button', { name: '更多“任务 A”操作' }));
    await user.click(screen.getByRole('menuitem', { name: '归档 Session' }));
    const rail = screen.getByRole('complementary', { name: 'Agent 工作记录' });
    await user.click(within(rail).getByRole('button', { name: /^任务 B/ }));
    expect(await screen.findByTestId('session-record-id')).toHaveTextContent('session-b');
    await act(async () => { archived.resolve({ ok: true }); await archived.promise; });
    expect(screen.getByTestId('session-record-id')).toHaveTextContent('session-b');
    expect(screen.queryByRole('textbox', { name: '和我的助手聊聊' })).not.toBeInTheDocument();
  });

  it('states the consequence of Session and previews Room partners as planets without persona names', async () => {
    const user = userEvent.setup();
    renderAgent();

    const composer = await screen.findByRole('textbox', { name: '描述你想完成的工作' });
    const briefId = composer.getAttribute('aria-describedby') ?? '';
    expect(briefId).not.toBe('');
    const sessionBrief = document.getElementById(briefId);
    expect(screen.getByRole('radio', { name: 'Session' })).toBeChecked();
    expect(sessionBrief).toHaveTextContent('随时补充想法，也可以暂停。');

    await user.click(screen.getByRole('radio', { name: 'Room' }));
    const roomBrief = within(document.getElementById(briefId) as HTMLElement);
    expect(roomBrief.getByText('任务建议 2 位')).toBeInTheDocument();
    expect(roomBrief.getByRole('group', { name: 'Room 伙伴数量' })).toHaveTextContent('2');
    expect(roomBrief.getByText('Earth')).toBeInTheDocument();
    expect(roomBrief.getByText('协调')).toBeInTheDocument();
    expect(roomBrief.getByText('Mars')).toBeInTheDocument();
    expect(roomBrief.getByText('审阅')).toBeInTheDocument();
    expect(roomBrief.queryByText('构建者')).not.toBeInTheDocument();
    expect(roomBrief.queryByText('审阅者')).not.toBeInTheDocument();
  });

  it('blocks a Room start with the truthful reason when fewer than two partners exist', async () => {
    const user = userEvent.setup();
    renderAgent(createTransport({ personas: [persona('builder', '构建者')] }));

    await user.type(await screen.findByRole('textbox', { name: '描述你想完成的工作' }), '只有一位伙伴');
    await user.click(screen.getByRole('radio', { name: 'Room' }));

    expect(screen.getByText('Room Runtime 当前要求至少 2 位伙伴；你可以预览 1 位，但需增加后才能开始。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '开始 Room' })).toBeDisabled();
  });

  it('projects the real catalog state in the Home footer and recovers with one reload', async () => {
    let modelCalls = 0;
    const transport = createTransport({
      modelCatalog: () => {
        modelCalls += 1;
        if (modelCalls === 1) throw new Error('目录服务不可用');
        return modelCatalog();
      },
    });
    const user = userEvent.setup();
    renderAgent(transport);

    expect(await screen.findByRole('alert')).toHaveTextContent('模型目录暂时无法读取。');
    expect(screen.queryByText(/Pi Runtime 已连接/)).not.toBeInTheDocument();
    expect(modelCalls).toBe(1);

    await user.click(screen.getByRole('button', { name: '重新读取模型' }));

    // Recovery is asynchronous; keep exact state and request-count assertions
    // with the same bounded deadline as Session model discovery under load.
    const model = await screen.findByRole('button', { name: '模型与推理：GPT-5.6 Luna · gpt · 高' }, { timeout: 5_000 });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(modelCalls).toBe(2);
    expect(model).toBeEnabled();
    await user.click(model);
    const picker = screen.getByRole('dialog', { name: '选择模型与推理强度' });
    await user.click(within(picker).getByRole('button', { name: /更换模型/ }));
    expect(within(picker).getAllByRole('option')).toHaveLength(3);
  });

  it('states the archived Session state on its revealed rail row', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    renderAgent(transport);
    await user.click(screen.getByRole('button', { name: '打开工作记录' }));
    const rail = screen.getByRole('complementary', { name: 'Agent 工作记录' });
    await user.click(await within(rail).findByRole('button', { name: '更多“发布检查”操作' }));
    await user.click(await screen.findByRole('menuitem', { name: '归档 Session' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: /^发布检查/ })).not.toBeInTheDocument());

    await user.click(screen.getByRole('button', { name: '工作记录选项' }));
    await user.click(await screen.findByRole('menuitemcheckbox', { name: '显示已归档 Session' }));

    const row = await within(rail).findByRole('button', { name: /^发布检查/ });
    expect(row).toHaveTextContent('已归档 · paw ·');
  });

  it('switches the same entry to Room and creates it with selected partners', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole('radio', { name: 'Room' }));
    await user.type(screen.getByRole('textbox', { name: '描述你想完成的工作' }), '并行检查前端与后端');
    await user.click(screen.getByRole('button', { name: '开始 Room' }));

    await waitFor(() => expect(transport.requests.map(({ request }) => request.pathId)).toEqual(
      expect.arrayContaining(['agent.rooms.create', 'agent.room.message']),
    ));
    const create = transport.requests.find(({ request }) => request.pathId === 'agent.rooms.create')?.request;
    expect(create?.body).toMatchObject({
      roomKind: 'collaboration',
      permissionPolicy: {
        schemaVersion: 'rag-ime.room-permission-policy.v1',
        room: { executionMode: 'full_trust' },
        partner: { executionMode: 'inherit' },
        toolAgent: { executionMode: 'inherit' },
      },
      dangerousModeConfirmation: 'ENABLE_FULL_TRUST',
      participants: expect.arrayContaining([
        expect.objectContaining({ roleId: 'builder' }),
        expect.objectContaining({ roleId: 'reviewer' }),
      ]),
    });
    expect(create?.body).not.toHaveProperty('executionMode');
    expect(await screen.findByText('Room 工作区 · room-new')).toBeInTheDocument();
  });

  it('selects a new workspace with the installed Electron host before starting a Room', async () => {
    const transport = createTransport();
    Object.defineProperty(transport, 'pickFiles', { configurable: true, value: undefined });
    const pickWorkspaceDirectory = vi.fn(async () => ({
      name: 'paw-natural',
      path: '/work/paw-natural',
    }));
    window.pawBrowserHost = {
      kind: 'electron-webview',
      partition: 'persist:paw-browser',
      pickWorkspaceDirectory,
    } as unknown as NonNullable<typeof window.pawBrowserHost>;
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole('button', { name: '起始项目 · work/paw' }));
    await user.click(await screen.findByRole('menuitem', { name: '浏览其他目录…' }));
    await waitFor(() => expect(pickWorkspaceDirectory).toHaveBeenCalledTimes(1));
    expect(screen.getByRole('button', { name: '起始项目 · work/paw-natural' })).toBeInTheDocument();

    await user.click(screen.getByRole('radio', { name: 'Room' }));
    await user.type(screen.getByRole('textbox', { name: '描述你想完成的工作' }), '从自然目标开始');
    await user.click(screen.getByRole('button', { name: '开始 Room' }));

    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'agent.rooms.create')).toBe(true));
    const create = transport.requests.find(({ request }) => request.pathId === 'agent.rooms.create')?.request;
    expect(create?.body).toMatchObject({ workspaceRoots: ['/work/paw-natural', '/'] });
  });

  it('opens the Room and its optimistic first message before a slow Room receipt settles', async () => {
    const roomMessage = deferred<unknown>();
    const transport = createTransport({ roomMessageHandler: () => roomMessage.promise });
    const user = userEvent.setup();
    renderAgent(transport);

    await user.click(await screen.findByRole('radio', { name: 'Room' }));
    await user.type(screen.getByRole('textbox', { name: '描述你想完成的工作' }), '立即进入 Room 现场');
    await user.click(screen.getByRole('button', { name: '开始 Room' }));

    await waitFor(() => expect(screen.getByText('Room 工作区 · room-new')).toBeInTheDocument());
    expect(transport.requests.map(({ request }) => request.pathId)).toContain('agent.room.message');
    roomMessage.resolve({ ok: true });
  });
});

type MockSessionSummary = {
  id: string;
  title: string;
  mode: string;
  status: string;
  updatedAtMs: number;
  workspaceRoots: string[];
  messageCount: number;
  lastMessagePreview: string;
  evaluationSnapshot?: boolean;
  roomParticipant?: boolean;
};

function agentTree(transport = createTransport(), props: { initialRoute?: string } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return (
    <QueryClientProvider client={client}>
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawAgentApp {...props} initialRoute={props.initialRoute ?? '/agent?new=advanced'} />
        </TooltipProvider>
      </ControlTransportProvider>
    </QueryClientProvider>
  );
}

function renderAgent(transport = createTransport(), props: { initialRoute?: string; target?: PawOsWindowTarget } = {}) {
  return render(agentTree(transport, props));
}

function catalogRequestPaths(transport: MockControlTransport): string[] {
  const catalogPaths = new Set([
    'agent.sessions.list',
    'agent.rooms.list',
    'agent.roles.list',
    'agent.role.models',
  ]);
  return transport.requests
    .map(({ request }) => request.pathId)
    .filter((pathId) => catalogPaths.has(pathId));
}

function createTransport(options: {
  archiveHandler?: (request: ControlRequest) => unknown | Promise<unknown>;
  promptHandler?: () => Promise<unknown>;
  modelHandler?: () => Promise<unknown>;
  modelCatalog?: unknown;
  personas?: unknown[];
  roomMessageHandler?: () => Promise<unknown>;
  staleCatalogAfterCreate?: boolean;
  preferencesHandler?: () => unknown | Promise<unknown>;
  rooms?: RoomSummary[];
  sessions?: MockSessionSummary[];
  primaryTasks?: unknown[];
  sessionCatalogHandler?: (request: ControlRequest) => unknown | Promise<unknown>;
} = {}) {
  let sessions: MockSessionSummary[] = options.sessions ?? [{
    id: 'session-old', title: '发布检查', mode: 'coordinator', status: 'idle', updatedAtMs: 2,
    workspaceRoots: ['/work/paw'], messageCount: 3, lastMessagePreview: '检查构建结果',
  }];
  let rooms: RoomSummary[] = options.rooms ?? [{
    id: 'room-old', title: '迁移作战室', status: 'active', roomKind: 'collaboration',
    routingPolicy: 'parallel', moderatorParticipantId: 'p1', updatedAtMs: 3,
    workspaceRoots: ['/work/paw'], participants: [
      { id: 'p1', sessionId: 's1', roleId: 'builder', roleVersion: '1', displayName: '构建者', status: 'active', ordinal: 0 },
      { id: 'p2', sessionId: 's2', roleId: 'reviewer', roleVersion: '1', displayName: '审阅者', status: 'active', ordinal: 1 },
    ],
  }];
  return new MockControlTransport({
    routes: {
      'agent.primary.ensure': { ok: true, session: { id: 'primary', title: '我的助手', status: 'idle', mode: 'assistant', updatedAtMs: 1, workspaceRoots: [], metadata: { primaryAssistant: true } }, tasks: options.primaryTasks ?? [] },
      'agent.sessions.list': options.sessionCatalogHandler ?? ((request: ControlRequest) => ({
        ok: true,
        items: request.query?.includeArchived ? sessions : sessions.filter((session) => session.status !== 'archived'),
      })),
      'agent.rooms.list': () => ({ ok: true, items: rooms }),
      'agent.room.get': (request: ControlRequest) => ({ ok: true, room: rooms.find(room => room.id === request.params?.roomId) ?? roomFixture({ id: request.params?.roomId }) }),
      'agent.jev.get': { ok: true, mode: 'jev', items: [] },
      'agent.roles.list': {
        ok: true,
        items: options.personas ?? [persona('builder', '构建者'), persona('reviewer', '审阅者')],
      },
      'agent.role.models': options.modelCatalog ?? { providers: [], selected: {} },
      'configuration.settings': options.preferencesHandler ?? preferenceSettings({}),
      'agent.sessions.create': (request: ControlRequest) => {
        const body = request.body as Record<string, unknown>;
        const created = { ...sessions[0], id: 'session-new', title: body.title as string, updatedAtMs: 4 };
        if (!options.staleCatalogAfterCreate) sessions = [created];
        return { ok: true, session: created };
      },
      'agent.session.prompt': options.promptHandler ?? { ok: true },
      'agent.session.archive': options.archiveHandler ?? ((request: ControlRequest) => {
        const sessionId = String(request.params?.sessionId ?? '');
        const archived = Boolean((request.body as Record<string, unknown>)?.archived);
        sessions = sessions.map((session) => session.id === sessionId
          ? { ...session, status: archived ? 'archived' : 'idle', updatedAtMs: session.updatedAtMs + 1 }
          : session);
        return { ok: true };
      }),
      'agent.session.delete': (request: ControlRequest) => {
        const sessionId = String(request.params?.sessionId ?? '');
        sessions = sessions.filter((session) => session.id !== sessionId);
        return { ok: true };
      },
      'agent.session.model.select': options.modelHandler ?? { ok: true },
      'agent.session.thinking.select': { ok: true },
      'agent.rooms.create': (request: ControlRequest) => {
        const body = request.body as Record<string, unknown>;
        const created = {
          ...rooms[0], id: 'room-new', title: body.title as string,
          participants: body.participants, updatedAtMs: 5,
        };
        rooms = [created as typeof rooms[number]];
        return { ok: true, room: created };
      },
      'agent.room.message': options.roomMessageHandler ?? { ok: true },
    },
  });
}

function roomFixture(overrides: Partial<RoomSummary> = {}): RoomSummary {
  return {
    id: 'room-old', title: '迁移作战室', status: 'active', roomKind: 'collaboration',
    routingPolicy: 'parallel', moderatorParticipantId: 'p1', updatedAtMs: 3,
    workspaceRoots: ['/work/paw'], participants: [
      { id: 'p1', sessionId: 's1', roleId: 'builder', roleVersion: '1', displayName: '构建者', status: 'active', ordinal: 0 },
      { id: 'p2', sessionId: 's2', roleId: 'reviewer', roleVersion: '1', displayName: '审阅者', status: 'active', ordinal: 1 },
    ],
    ...overrides,
  };
}

function workItemFixture(overrides: Partial<RoomWorkItem> = {}): RoomWorkItem {
  return {
    id: 'work-1', roomId: 'room-old', topicId: 'topic-1', rootTurnId: 'turn-1', rootWorkId: 'work-1',
    parentWorkId: '', objective: '完成任务', expectedOutput: '结果', acceptanceCriteria: [],
    accountableParticipantId: 'p1', currentOwnerParticipantId: 'p1', offeredToParticipantId: '',
    createdByParticipantId: 'p1', clientMessageId: 'message-1', state: 'active', depth: 0, revision: 1,
    resultSummary: '', artifactRefs: [], evidenceRefs: [], blocker: {}, acceptedTurnId: 'turn-1',
    createdAtMs: 1, updatedAtMs: 1, completedAtMs: null,
    ...overrides,
  };
}

function modelCatalog() {
  return {
    providers: [{
      id: 'gpt',
      models: [
        { provider: 'gpt', id: 'gpt-5.6-luna', name: 'GPT-5.6 Luna', thinkingLevels: ['off', 'high', 'max'] },
        { provider: 'gpt', id: 'gpt-5.6-sol', name: 'GPT-5.6 Sol', thinkingLevels: ['off', 'high', 'max'] },
        { provider: 'gpt', id: 'gpt-5.6-terra', name: 'GPT-5.6 Terra', thinkingLevels: ['off', 'high', 'max'] },
      ],
    }],
    selected: { provider: 'gpt', id: 'gpt-5.6-luna' },
  };
}

function preferenceSettings(overrides: Partial<{
  modelReference: string;
  thinkingLevel: string;
  executionMode: string;
}>) {
  return {
    ok: true,
    settings: {
      agent: {
        defaults: {
          modelReference: 'inherit',
          thinkingLevel: 'high',
          executionMode: 'per_action',
          ...overrides,
        },
      },
    },
    runtimeConfig: { runtimeRevision: 7 },
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => { resolve = next; });
  return { promise, resolve };
}

function persona(roleId: string, displayName: string) {
  return {
    schemaVersion: 'rag-ime.agent-persona.v1', roleId, version: '1', displayName,
    tagline: `${displayName}伙伴`, description: '', selectableModes: ['assistant', 'coordinator'],
    defaults: { mode: 'coordinator', executionMode: 'per_action', modelPolicy: 'inherit', thinkingLevel: 'high', toolProfileVersion: 'control-center-v1', toolAllowlistMode: 'profile' },
    runtimeCharacteristics: { isDefault: false, canReadWorkspace: true, canWriteWorkspace: true, canUseTools: true },
    visualProfile: { accentToken: 'violet', avatar: 'bot' },
    content: { systemPrompt: '', boundaries: [], firstMessage: '' }, status: 'active', createdAtMs: 1, updatedAtMs: 1,
  };
}
