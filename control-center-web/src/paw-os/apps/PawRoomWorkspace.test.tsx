import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ControlTransportProvider } from '@/app/control-transport';
import { createPreviewTransport } from '@/app/preview-control-transport';
import { previewRoomSnapshot } from '@/app/preview-room-data';
import { TooltipProvider } from '@/components/primitives';
import { PawOsDesktopProvider } from '@/features/paw-os/surface-context';
import { ROOM_WORKSPACE_MISSING_TEXT } from '@/features/agent/public-error';
import { parseRoomEvent } from '@/contracts/validators';
import type { ControlRequest } from '@/platform/transport';
import type { RoomSummary } from '@/features/rooms/room-types';
import { useRoomLiveStore } from '@/features/rooms/state/live-store';
import { PawWindowFrame } from '../shell/PawWindowLayer';
import { PawRoomWorkspace } from './PawRoomWorkspace';
import { recoveryScope } from '@/features/semantic-workspace/workspace-recovery';
import roomFocusCss from '../styles/paw-os-room-focus.css?raw';

/* Lazy-bundle proof: this flag flips only when the PawStarfield module is
 * actually evaluated. Rendering the Room conversation must never flip it;
 * only pressing the 星空 button may. */
const starfieldChunk = vi.hoisted(() => ({ evaluated: false }));
vi.mock('./PawStarfield', async (importOriginal) => {
  starfieldChunk.evaluated = true;
  return await importOriginal();
});

afterEach(() => {
  cleanup();
  window.localStorage.removeItem('pawos.room-observer-auto-open.v1');
  window.localStorage.removeItem('pawos.room-work-status-visible.v1');
  useRoomLiveStore.getState().reset();
  for (const key of Object.keys(localStorage)) if (key.endsWith(':view:v2') || key.startsWith('paw.workspace.draft.v1:room-workspace-test')) localStorage.removeItem(key);
});

describe('PAWOS Room collaboration tools', () => {
  it('recovers a queued follow-up rejected asynchronously alongside the newer composer draft', async () => {
    const source = previewRoomSnapshot('room-queued-refusal');
    const room = { ...source.room, workItems: [], lastEventSequence: 4 };
    const snapshot = { ...source, room, events: source.events.slice(0, 4), lastSequence: 4, resumeToken: `${room.id}:4` };
    const mounted = renderRoom(900, vi.fn(), room as unknown as RoomSummary, snapshot,
      undefined, undefined, vi.fn(), request => {
        if (request.pathId === 'agent.room.message') throw Object.assign(new Error('synthetic pre-admission rejection'), { status: 422 });
        return undefined;
      });
    const editor = await screen.findByRole('textbox', { name: '协作消息' });
    await screen.findByText('当前任务仍在执行。现在发送文字会立即干预主持伙伴的当前回合。');
    fireEvent.change(editor, { target: { value: '不能丢失的排队补充' } });
    fireEvent.click(screen.getByRole('button', { name: '排到当前回合之后' }));
    fireEvent.change(editor, { target: { value: '下一项任务的新草稿' } });
    act(() => useRoomLiveStore.setState(state => {
      const projection = state.projections[room.id]!;
      const rootId = `${room.id}:turn-1`;
      return { projections: { ...state.projections, [room.id]: { ...projection, turnsById: {
        ...projection.turnsById, [rootId]: { ...projection.turnsById[rootId]!, status: 'completed' as const },
      } } } };
    }));
    await waitFor(() => expect(mounted.transport.requests.filter(({ request }) => request.pathId === 'agent.room.message')).toHaveLength(1));
    await waitFor(() => expect(editor).toHaveValue('下一项任务的新草稿\n\n不能丢失的排队补充'));
    expect(mounted.transport.requests.filter(({ request }) => request.pathId === 'agent.room.message')).toHaveLength(1);
  });

  it.each(['accepted', 'rejected'] as const)('keeps newer draft attachments separate from a queued %s delivery across remount', async outcome => {
    const source = previewRoomSnapshot(`room-queued-${outcome}-remount`);
    const room = { ...source.room, workItems: [], lastEventSequence: 4 };
    const snapshot = { ...source, room, events: source.events.slice(0, 4), lastSequence: 4, resumeToken: `${room.id}:4` };
    let release: (() => void) | undefined;
    const mounted = renderRoom(900, vi.fn(), room as unknown as RoomSummary, snapshot,
      undefined, undefined, vi.fn(), request => request.pathId !== 'agent.room.message' ? undefined : new Promise<Record<string, unknown>>((resolve, reject) => {
        release = () => outcome === 'accepted' ? resolve({ ok: true, accepted: true }) : reject(Object.assign(new Error('synthetic refusal'), { status: 422 }));
      }));
    const editor = await screen.findByRole('textbox', { name: '协作消息' });
    await screen.findByText('当前任务仍在执行。现在发送文字会立即干预主持伙伴的当前回合。');
    fireEvent.change(editor, { target: { value: '排队文字' } });
    fireEvent.click(screen.getByRole('button', { name: '排到当前回合之后' }));
    fireEvent.change(editor, { target: { value: '新草稿' } });
    fireEvent.paste(editor, { clipboardData: { files: [new File(['png'], 'next.png', { type: 'image/png' })], items: [], getData: () => '' } });
    await screen.findByLabelText('移除 next.png');
    act(() => useRoomLiveStore.setState(state => {
      const projection = state.projections[room.id]!; const rootId = `${room.id}:turn-1`;
      return { projections: { ...state.projections, [room.id]: { ...projection, turnsById: {
        ...projection.turnsById, [rootId]: { ...projection.turnsById[rootId]!, status: 'completed' as const },
      } } } };
    }));
    await waitFor(() => expect(release).toBeTypeOf('function'));
    expect(mounted.transport.requests.find(({ request }) => request.pathId === 'agent.room.message')?.request.body).toMatchObject({ message: '排队文字', attachmentIds: [] });
    expect(editor).toHaveValue('新草稿');
    mounted.remount();
    const reopened = await screen.findByRole('textbox', { name: '协作消息' });
    await act(async () => release!());
    await waitFor(() => expect(reopened).toHaveValue(outcome === 'accepted' ? '新草稿' : '新草稿\n\n排队文字'));
    expect(screen.getByLabelText('移除 next.png')).toBeInTheDocument();
    expect(mounted.transport.requests.filter(({ request }) => request.pathId === 'agent.room.message')).toHaveLength(1);
  });

  it('retains an unsent follow-up when the Room is archived before its turn settles', async () => {
    const source = previewRoomSnapshot('room-queued-archive');
    const room = { ...source.room, workItems: [], lastEventSequence: 4 };
    const snapshot = { ...source, room, events: source.events.slice(0, 4), lastSequence: 4, resumeToken: `${room.id}:4` };
    const mounted = renderRoom(900, vi.fn(), room as unknown as RoomSummary, snapshot);
    const editor = await screen.findByRole('textbox', { name: '协作消息' });
    await screen.findByText('当前任务仍在执行。现在发送文字会立即干预主持伙伴的当前回合。');
    fireEvent.change(editor, { target: { value: '请保留这条未发送的补充' } });
    fireEvent.click(screen.getByRole('button', { name: '排到当前回合之后' }));
    expect(screen.getByRole('status', { name: '等待当前执行完成后发送的消息' })).toHaveTextContent('请保留这条未发送的补充');
    mounted.room.status = 'archived';
    mounted.setDesktopFocusGroup(undefined);
    act(() => useRoomLiveStore.setState(state => {
      const projection = state.projections[room.id]!;
      const rootId = `${room.id}:turn-1`;
      return { projections: { ...state.projections, [room.id]: { ...projection, turnsById: {
        ...projection.turnsById, [rootId]: { ...projection.turnsById[rootId]!, status: 'completed' as const },
      } } } };
    }));
    await waitFor(() => expect(screen.queryByText('当前任务仍在执行。现在发送文字会立即干预主持伙伴的当前回合。')).not.toBeInTheDocument());
    expect(screen.getByRole('status', { name: '等待当前执行完成后发送的消息' })).toHaveTextContent('请保留这条未发送的补充');
    expect(mounted.transport.requests.some(({ request }) => request.pathId === 'agent.room.message')).toBe(false);
  });

  it('reads completed requests and follow-ups as one continuous Room conversation by default', async () => {
    const first = previewRoomSnapshot('room-continuous-default');
    const rootId = `${first.room.id}:turn-2`;
    const events = [...first.events,
      { ...first.events[0]!, eventId: `${first.room.id}:followup`, sequence: first.lastSequence + 1, turnId: rootId,
        payload: { rootId, messageId: 'room-user-followup', text: '继续补充验收记录，沿用之前的上下文。' } },
      { ...first.events.at(-1)!, eventId: `${first.room.id}:followup-completed`, sequence: first.lastSequence + 2, turnId: rootId,
        payload: { rootId, summary: '补充已完成' } },
    ];
    const snapshot = { ...first, events, lastSequence: events.length, room: { ...first.room, lastEventSequence: events.length } };
    const mounted = renderRoom(900, vi.fn(), snapshot.room as unknown as RoomSummary, snapshot, undefined, undefined, vi.fn(), undefined, undefined, true, false, 'session-window', undefined, 'default');
    await screen.findByRole('textbox', { name: '协作消息' });
    const conversation = await screen.findByRole('region', { name: 'Room 公开对话' });
    expect(conversation).toBeInTheDocument();
    await waitFor(() => expect(useRoomLiveStore.getState().projections[snapshot.room.id]?.turnOrder).toHaveLength(2));
    expect(screen.queryByRole('navigation', { name: '对话轮次' })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Room 行星任务表' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '对话' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByRole('button', { name: '执行记录' }));
    expect(await screen.findByRole('region', { name: 'Room 行星任务表' })).toBeInTheDocument();
    mounted.remount();
    expect(await screen.findByRole('region', { name: 'Room 行星任务表' })).toBeInTheDocument();
    expect(mounted.transport.requests.some(({ request }) => request.pathId === 'agent.room.message')).toBe(false);
  });

  it('uses one collaboration surface for task and message entry points without reviving the retired overview', async () => {
    const mounted = renderRoom(900);
    await screen.findByRole('textbox', { name: '协作消息' });
    fireEvent.click(screen.getByRole('button', { name: '查看 Room 任务' }));
    const collaboration = await screen.findByRole('region', { name: 'Room 协作' });
    expect(within(collaboration).getByRole('tab', { name: '任务与回执' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getAllByRole('region', { name: 'Room 协作' })).toHaveLength(1);
    fireEvent.click(within(collaboration).getByRole('tab', { name: '时间线' }));
    fireEvent.click(screen.getByRole('button', { name: '查看 Room 任务' }));
    expect(within(collaboration).getByRole('tab', { name: '任务与回执' })).toHaveAttribute('aria-selected', 'true');

    expect(screen.queryByRole('region', { name: 'Sol 协作态势' })).not.toBeInTheDocument();
    expect(document.querySelector('[style*="starfield/"]')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '消息流' }));
    expect(await screen.findByRole('tab', { name: '消息往返' })).toHaveAttribute('aria-selected', 'true');
    fireEvent.click(within(collaboration).getByRole('tab', { name: '时间线' }));
    fireEvent.click(screen.getByRole('button', { name: '消息流' }));
    expect(within(collaboration).getByRole('tab', { name: '消息往返' })).toHaveAttribute('aria-selected', 'true');

    expect(screen.getByRole('region', { name: '往来记录' })).toBeInTheDocument();
    expect(screen.queryByRole('group', { name: '任务与关系详情' })).not.toBeInTheDocument();
    mounted.remount();
    expect(await screen.findByRole('tab', { name: '消息往返' })).toHaveAttribute('aria-selected', 'true');

    expect(mounted.transport.requests.some(({ request }) => request.pathId === 'agent.room.message')).toBe(false);
  });

  it('presents a queued Root honestly while supplements still steer that exact Root, then shows running evidence', async () => {
    const source = previewRoomSnapshot('room-queued-copy');
    const room = { ...source.room, workItems: [], lastEventSequence: 1 };
    const snapshot = { ...source, room, events: source.events.slice(0, 1), lastSequence: 1, resumeToken: 'room-queued-copy:1' };
    const mounted = renderRoom(900, vi.fn(), room as unknown as RoomSummary, snapshot, undefined, undefined, vi.fn(), request =>
      request.pathId === 'agent.room.participant.steer' ? { ok: true, accepted: true } : undefined);
    const editor = await screen.findByRole('textbox', { name: '协作消息' });
    await waitFor(() => expect(useRoomLiveStore.getState().snapshotsByRoomId[room.id]).toBeDefined());
    // Public UI projection seam: a queued Root is a distinct input from the running event snapshot.
    act(() => useRoomLiveStore.setState(state => {
      const projection = state.projections[room.id]!;
      const rootId = 'room-queued-copy:turn-1';
      return { projections: { ...state.projections, [room.id]: { ...projection, turnsById: { ...projection.turnsById,
        [rootId]: { ...projection.turnsById[rootId]!, status: 'queued' as const },
      } } } };
    }));
    expect(within(screen.getByRole('region', { name: '协作状态' })).getByText('请求已排队')).toBeInTheDocument();
    expect(screen.getByText('请求已排队，正在等待开始；可补充要求，或将新消息排到下一轮。')).toBeInTheDocument();
    expect(screen.queryByText(/^当前任务仍在执行/u)).not.toBeInTheDocument();
    fireEvent.change(editor, { target: { value: '补充验收边界' } });
    fireEvent.click(screen.getByRole('button', { name: '补充当前请求' }));
    await waitFor(() => expect(mounted.transport.requests.filter(({ request }) => request.pathId === 'agent.room.participant.steer')).toHaveLength(1));
    expect(mounted.transport.requests.find(({ request }) => request.pathId === 'agent.room.participant.steer')?.request.body).toMatchObject({
      rootId: 'room-queued-copy:turn-1', participantId: 'participant-present', message: '补充验收边界',
    });
    expect(mounted.transport.requests.some(({ request }) => request.pathId === 'agent.room.message')).toBe(false);
    // The existing recovery owner replaces the queued UI projection with a full, progressed snapshot.
    snapshot.events = source.events.slice(0, 4); snapshot.lastSequence = 4;
    snapshot.room.lastEventSequence = 4; snapshot.resumeToken = 'room-queued-copy:4';
    act(() => mounted.controlTransport.emit('agent.room.events', { ...source.events[0],
      eventType: 'snapshot_required', payload: { reason: 'cursor_gap' },
    }));
    expect(await screen.findByText('当前任务仍在执行。现在发送文字会立即干预主持伙伴的当前回合。')).toBeInTheDocument();
    expect(editor).toHaveAttribute('placeholder', '立即干预当前回合…');
    fireEvent.change(editor, { target: { value: '继续保留验收边界' } });
    expect(screen.getByRole('button', { name: '立即干预当前回合' })).toBeEnabled();
  });

  it('restores the default round scroller within its connection-scoped Room reading owner', async () => {
    const source = previewRoomSnapshot('room-reading-workspace');
    vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(1600);
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(500);
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      const top = this.dataset.roundId ? 100 - ((this.closest('.paw-room-rounds') as HTMLElement | null)?.scrollTop ?? 0) : 100;
      return { top, bottom: top + 1600, height: 1600, left: 0, right: 700, width: 700, x: 0, y: top, toJSON: () => ({}) };
    });
    try {
      const mounted = renderRoom(900, vi.fn(), source.room as unknown as RoomSummary, source);
      Object.defineProperty(mounted.controlTransport, 'connectionIdentity', { value: 'reading-workspace-backend' });
      localStorage.setItem(recoveryScope(mounted.controlTransport, `room:${source.room.id}`) + ':view:v2', 'rounds');
      mounted.remount();
      await waitFor(() => expect(useRoomLiveStore.getState().snapshotsByRoomId[source.room.id]).toBeDefined());
      const scroller = await screen.findByRole('region', { name: 'Room 行星任务表' });
      fireEvent.wheel(scroller, { deltaY: -100 });
      fireEvent.scroll(scroller, { target: { scrollTop: 200 } });
      mounted.remount();
      const restored = await screen.findByRole('region', { name: 'Room 行星任务表' });
      await waitFor(() => expect(restored.scrollTop).toBe(200));
      expect(restored).not.toBe(scroller);
      expect(mounted.transport.requests.some(({ request }) => request.pathId === 'agent.room.message')).toBe(false);
    } finally {
      cleanup(); vi.restoreAllMocks();
      for (const key of Object.keys(localStorage)) if (key.includes('reading-workspace-backend')) localStorage.removeItem(key);
    }
  });

  it('remembers the composer popup choice and gates automatic observers without sending', async () => {
    const user = userEvent.setup(); const openWindow = vi.fn();
    window.localStorage.setItem('pawos.room-observer-auto-open.v1', 'off');
    const { transport } = renderRoom(900, openWindow);
    await screen.findByRole('textbox', { name: '协作消息' });
    const toggle = screen.getByRole('switch', { name: '伙伴窗口自动弹出' });
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    await user.click(screen.getByRole('button', { name: '协同模式' }));
    expect(openWindow).not.toHaveBeenCalled();
    await user.click(toggle);
    await waitFor(() => expect(openWindow).toHaveBeenCalled());
    expect(toggle).toHaveAttribute('aria-checked', 'true');
    expect(window.localStorage.getItem('pawos.room-observer-auto-open.v1')).toBe('on');
    openWindow.mockClear();
    await user.click(toggle);
    expect(openWindow).not.toHaveBeenCalled();
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.room.message')).toHaveLength(0);
    expect(window.localStorage.getItem('pawos.room-observer-auto-open.v1')).toBe('off');
  });

  it('hides and restores the task status dock independently of popup and execution controls', async () => {
    const user = userEvent.setup(); const { transport } = renderRoom(900);
    await screen.findByRole('textbox', { name: '协作消息' });
    await waitFor(() => expect(screen.getByLabelText('协作状态')).toBeVisible());
    const toggle = screen.getByRole('switch', { name: '显示任务状态栏' });
    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    expect(screen.queryByLabelText('协作状态')).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '协作消息' })).toBeEnabled();
    expect(screen.getByRole('switch', { name: '伙伴窗口自动弹出' })).toHaveAttribute('aria-checked', 'true');
    expect(window.localStorage.getItem('pawos.room-work-status-visible.v1')).toBe('off');
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.room.message')).toHaveLength(0);
    await user.click(toggle);
    expect(screen.getByLabelText('协作状态')).toBeVisible();
    expect(window.localStorage.getItem('pawos.room-work-status-visible.v1')).toBe('on');
  });

  it('restores the saved hidden status dock without hiding the composer', async () => {
    window.localStorage.setItem('pawos.room-work-status-visible.v1', 'off');
    renderRoom(900);
    await screen.findByRole('textbox', { name: '协作消息' });
    expect(screen.getByRole('switch', { name: '显示任务状态栏' })).toHaveAttribute('aria-checked', 'false');
    expect(screen.queryByLabelText('协作状态')).not.toBeInTheDocument();
  });

  it('shows recovery rather than an empty first round before the initial snapshot arrives', async () => {
    renderRoom(900);
    expect(screen.queryByText('等待第一轮任务')).not.toBeInTheDocument();
    expect(screen.getByRole('status', { name: '正在恢复 Room 协作现场' })).toBeInTheDocument();
    await screen.findByRole('textbox', { name: '协作消息' });
  });

  it('shows a cancelled Root as stopped and makes the composer ready for a new round', async () => {
    const source = previewRoomSnapshot('room-cancelled-root');
    const terminal = {
      ...source.events[0],
      sequence: 4, eventId: `${source.room.id}:4`, resumeToken: `${source.room.id}:4`,
      eventType: 'participant_status', participantId: null, sourceSessionId: '',
      payload: {
        status: 'cancellation_applied', rootId: source.events[0].turnId,
        cancellationReceiptId: 'cancel-1', pendingTargets: [],
      },
    };
    const snapshot = {
      ...source, events: [...source.events.slice(0, 3), terminal],
      room: { ...source.room, lastEventSequence: 4 }, lastSequence: 4,
      resumeToken: `${source.room.id}:4`,
    };
    renderRoom(900, vi.fn(), snapshot.room as unknown as RoomSummary, snapshot);
    await waitFor(() => expect(useRoomLiveStore.getState().projections[source.room.id]?.lastSequence).toBe(4));
    expect(document.querySelector('.paw-room-workspace__runtime')).toHaveTextContent('本轮已停止');
    expect(screen.queryByRole('button', { name: '停止整轮协作' })).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '协作消息' })).not.toHaveAttribute('placeholder', '立即干预当前回合…');
  });

  it.each(['completed', 'failed'] as const)('preserves a %s Root when its pending Stop reports already_terminal', async (status) => {
    const source = previewRoomSnapshot(`room-stop-race-${status}`);
    const snapshot = { ...source, events: source.events.slice(0, 3),
      room: { ...source.room, lastEventSequence: 3 }, lastSequence: 3, resumeToken: `${source.room.id}:3` };
    const { controlTransport, transport } = renderRoom(900, vi.fn(), snapshot.room as unknown as RoomSummary, snapshot);
    const originalRequest = controlTransport.request.bind(controlTransport);
    let resolveAbort!: (response: Record<string, unknown>) => void;
    const abortResponse = new Promise<Record<string, unknown>>(resolve => { resolveAbort = resolve; });
    controlTransport.request = async <Response = unknown>(request: ControlRequest): Promise<Response> => {
      if (request.pathId === 'agent.room.abort') return await abortResponse as Response;
      return originalRequest<Response>(request);
    };
    await userEvent.click(await screen.findByRole('button', { name: '停止整轮协作' }));
    const rootId = source.events[0]!.turnId;
    act(() => useRoomLiveStore.getState().applyEvents(source.room.id, [parseRoomEvent({
      ...source.events[0], sequence: 4, eventId: `${source.room.id}:4`, resumeToken: `${source.room.id}:4`,
      eventType: status === 'failed' ? 'turn_failed' : 'turn_completed',
      participantId: null, sourceSessionId: '', payload: { status },
    })]));
    const before = useRoomLiveStore.getState().projections[source.room.id]!;
    await act(async () => resolveAbort({ ok: true, status: 'already_terminal' }));
    await waitFor(() => expect(transport.requests.filter(call => call.request.pathId === 'agent.room.snapshot')).toHaveLength(2));
    expect(useRoomLiveStore.getState().projections[source.room.id]).toBe(before);
    expect(before.turnsById[rootId]?.status).toBe(status);
    expect(screen.queryByRole('button', { name: '停止整轮协作' })).not.toBeInTheDocument();
    expect(document.querySelector('.paw-room-workspace__runtime')).not.toHaveTextContent('本轮已停止');
  });

  it('lets a stale Room replace its workspace instead of retrying an impossible sync', async () => {
    const { controlTransport, room, transport } = renderRoom(
      900,
      vi.fn(),
      undefined,
      undefined,
      undefined,
      undefined,
      vi.fn(),
      undefined,
      ROOM_WORKSPACE_MISSING_TEXT,
    );
    const pickFiles = vi.spyOn(controlTransport, 'pickFiles').mockResolvedValue([{
      id: 'room-workspace-rebound',
      name: 'rebound',
      path: '/work/rebound',
      mimeType: 'application/x-directory',
      byteSize: 0,
    }]);
    const request = controlTransport.request.bind(controlTransport);
    controlTransport.request = async <Response = unknown>(input: ControlRequest): Promise<Response> => {
      if (input.pathId === 'agent.room.archive') {
        transport.requests.push({ request: input });
        return {
          ok: true,
          room: { ...room, workspaceRoots: ['/work/rebound'] },
        } as Response;
      }
      return request<Response>(input);
    };

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('这个 Room 的工作目录已不存在');
    expect(within(alert).queryByRole('button', { name: '重新同步' })).not.toBeInTheDocument();
    await userEvent.setup().click(within(alert).getByRole('button', { name: '选择工作目录' }));
    await waitFor(() => expect(pickFiles).toHaveBeenCalledWith(expect.objectContaining({
      purpose: 'workspace-root',
      selection: 'directory',
    })));
    await waitFor(() => expect(transport.requests.find(({ request: item }) => (
      item.pathId === 'agent.room.archive'
    ))?.request).toMatchObject({
      body: { workspaceRoots: ['/work/rebound'] },
    }));
  });

  it('hydrates a planet mention and prewarms its Session', async () => {
    const { room, transport } = renderRoom(900, vi.fn(), undefined, undefined, '@Mars ');
    const mars = room.participants.find((participant) => participant.displayName === 'Mars');

    expect(await screen.findByRole('textbox', { name: '协作消息' })).toHaveValue('@Mars ');
    await waitFor(() => expect(transport.requests.some(({ request }) => {
      const body = request.body;
      return request.pathId === 'agent.runtime.ensure'
        && typeof body === 'object'
        && body !== null
        && !Array.isArray(body)
        && 'sessionId' in body
        && body.sessionId === mars?.sessionId;
    })).toBe(true));
  });

  it('prewarms the moderator Session while a Room is visible', async () => {
    const { room, transport } = renderRoom(900);
    const moderator = room.participants.find((participant) => (
      participant.id === room.moderatorParticipantId
    ));

    await waitFor(() => expect(transport.requests.find(({ request }) => (
      request.pathId === 'agent.runtime.ensure'
    ))?.request).toMatchObject({
      body: { sessionId: moderator?.sessionId },
    }));
  });

  it('keeps a rejected busy message as a draft without presenting an offline connection', async () => {
    const user = userEvent.setup();
    const { transport } = renderRoom(
      900, vi.fn(), undefined, undefined, undefined, undefined, vi.fn(),
      (request) => {
        if (request.pathId !== 'agent.room.message') return undefined;
        throw Object.assign(new Error('Agent 3 is currently busy'), {
          payload: {
            code: 'AGENT_COMMAND_FAILED',
            commandReceipt: { state: 'failed', clientMessageId: (request.body as { clientMessageId: string }).clientMessageId, causeCode: 'ROOM_PARTICIPANT_BUSY' },
          },
        });
      },
    );
    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    await user.type(composer, '是什么问题呀');
    await user.click(screen.getByRole('button', { name: '发送消息' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('目标伙伴正在处理另一条请求');
    expect(composer).toHaveValue('是什么问题呀');
    expect(document.querySelector('.paw-room-workspace__runtime')).not.toHaveTextContent('同步离线');
    expect(within(alert).queryByRole('button', { name: '重新同步' })).not.toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.room.message')).toHaveLength(1);
  });

  it('shows submitted partner results separately while the Room still awaits its Root terminal', async () => {
    const source = previewRoomSnapshot('room-partner-results');
    const events = source.events.slice(0, 13);
    const room = { ...source.room, lastEventSequence: 13 };
    const snapshot = {
      ...source,
      room,
      events,
      lastSequence: 13,
      resumeToken: 'room-partner-results:13',
    };

    renderRoom(900, vi.fn(), room as unknown as RoomSummary, snapshot);

    await screen.findByRole('textbox', { name: '协作消息' });
    await waitFor(() => expect(useRoomLiveStore.getState().projections[source.room.id]?.lastSequence).toBe(13));
    expect(document.querySelector('.paw-room-workspace__runtime')).toHaveTextContent('1 个工作项等待复核');
    if (screen.queryByRole('button', { name: '展开 Room 控件' })) fireEvent.click(screen.getByRole('button', { name: '展开 Room 控件' }));
    expect(screen.getByRole('region', { name: 'Room 当前协作' })).toHaveTextContent('2 伙伴执行结束');
    expect(screen.queryByText('本轮执行已结束')).not.toBeInTheDocument();
    const rounds = screen.getByRole('region', { name: 'Room 行星任务表' });
    expect(within(rounds).getByRole('region', { name: 'Earth 主控回复' })).toHaveTextContent(
      '我已把实时进展收拢在同一条消息里',
    );
    expect(within(rounds).queryByRole('region', { name: 'Earth 最终结果' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '停止整轮协作' })).not.toBeInTheDocument();
  });

  it('labels a Room sync failure offline while retaining the last Room metadata', async () => {
    const source = previewRoomSnapshot('room-offline');
    renderRoom(
      900,
      vi.fn(),
      source.room as unknown as RoomSummary,
      undefined,
      undefined,
      undefined,
      vi.fn(),
      undefined,
      undefined,
      true,
      true,
    );

    await waitFor(() => expect(document.querySelector('.paw-room-workspace__runtime')).toHaveTextContent('连接中断 · 显示上次状态'));
    expect(document.querySelector('.paw-room-workspace')).toHaveAttribute('data-status', 'failed');
    expect(screen.getByRole('alert')).toBeInTheDocument();
    if (screen.queryByRole('button', { name: '展开 Room 控件' })) fireEvent.click(screen.getByRole('button', { name: '展开 Room 控件' }));
    expect(screen.getByRole('region', { name: 'Room 当前协作' })).toHaveTextContent('任务图依赖验证');
    expect(screen.queryByRole('status', { name: '正在恢复 Room 协作现场' })).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Room 记录暂时不可用' })).toHaveTextContent('重新同步');
  });

  it('recovers the first Room record without resending the preserved draft', async () => {
    const user = userEvent.setup();
    const initialRoom = previewRoomSnapshot('room-first-snapshot-retry').room as unknown as RoomSummary;
    const { controlTransport, room, transport } = renderRoom(
      900, vi.fn(), initialRoom, undefined, '还没发送的目标', undefined,
      vi.fn(), undefined, undefined, true, true,
    );
    const unavailable = await screen.findByRole('region', { name: 'Room 记录暂时不可用' });
    const failedRequest = controlTransport.request.bind(controlTransport);
    controlTransport.request = async <Response = unknown>(request: ControlRequest): Promise<Response> => {
      if (request.pathId === 'agent.room.snapshot') return previewRoomSnapshot(room.id) as Response;
      return failedRequest<Response>(request);
    };

    await user.click(within(unavailable).getByRole('button', { name: '重新同步' }));

    await waitFor(() => expect(screen.queryByRole('region', { name: 'Room 记录暂时不可用' })).not.toBeInTheDocument());
    expect(screen.getByRole('textbox', { name: '协作消息' })).toHaveValue('还没发送的目标');
    expect(screen.queryByRole('status', { name: '正在恢复 Room 协作现场' })).not.toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.room.message')).toBe(false);
  });

  it('retries a lost Room ACK with the original request instead of starting a second task', async () => {
    const user = userEvent.setup();
    let attempts = 0;
    const { transport, remount } = renderRoom(900, vi.fn(), undefined, undefined, undefined, undefined, vi.fn(), (request) => {
      if (request.pathId !== 'agent.room.message') return undefined;
      attempts += 1;
      if (attempts === 1) throw new TypeError('ACK connection lost after commit');
      return { ok: true, accepted: true, idempotentReplay: true };
    });
    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    fireEvent.paste(composer, {
      clipboardData: { files: [new File(['png'], 'retry.png', { type: 'image/png' })], items: [], getData: () => '' },
    });
    expect(await screen.findByLabelText('移除 retry.png')).toBeInTheDocument();
    await user.type(composer, '只执行一次');
    await user.click(screen.getByRole('button', { name: '发送消息' }));
    await waitFor(() => expect(composer).toHaveValue('只执行一次'));
    await user.click(screen.getByRole('button', { name: '发送消息' }));
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.room.message')).toHaveLength(1);
    remount();
    const reopenedComposer = await screen.findByRole('textbox', { name: '协作消息' });
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.room.message')).toHaveLength(1);
    await user.clear(reopenedComposer);
    await user.type(reopenedComposer, '这是另一项任务');
    await user.click(screen.getByRole('button', { name: '核实上次发送' }));
    const requests = transport.requests.filter(({ request }) => request.pathId === 'agent.room.message');
    expect(requests).toHaveLength(2);
    expect(requests[1].request).toEqual(requests[0].request);
    expect(reopenedComposer).toHaveValue('这是另一项任务');
  });

  it('keeps the next draft and its image out of Continue and its explicit lost-ACK recovery', async () => {
    const source = previewRoomSnapshot('room-continue-draft');
    const terminal = {
      ...source.events[0], sequence: 4, eventId: `${source.room.id}:4`, resumeToken: `${source.room.id}:4`,
      eventType: 'turn_failed', participantId: null, sourceSessionId: '',
      payload: { status: 'failed', rootId: source.events[0].turnId, error: 'synthetic provider failure' },
    };
    const snapshot = { ...source, events: [...source.events.slice(0, 3), terminal],
      room: { ...source.room, lastEventSequence: 4 }, lastSequence: 4, resumeToken: `${source.room.id}:4` };
    let calls = 0;
    const { transport } = renderRoom(900, vi.fn(), snapshot.room as unknown as RoomSummary, snapshot,
      undefined, undefined, vi.fn(), (request) => {
        if (request.pathId !== 'agent.room.message') return undefined;
        if (++calls === 1) throw new TypeError('synthetic lost ACK');
        return { ok: true, idempotentReplay: true };
      });
    const user = userEvent.setup();
    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    await user.type(composer, '下一条消息的草稿');
    fireEvent.paste(composer, {
      clipboardData: { files: [new File(['png'], 'next-draft.png', { type: 'image/png' })], items: [], getData: () => '' },
    });
    expect(await screen.findByLabelText('移除 next-draft.png')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '对话' }));
    await user.click(screen.getByRole('button', { name: '继续' }));
    await screen.findByRole('button', { name: '核实上次发送' });
    const first = transport.requests.find(({ request }) => request.pathId === 'agent.room.message')!.request;
    expect(first.body).toMatchObject({ attachmentIds: [] });
    expect((first.body as Record<string, unknown>).message).toContain('不要重复已经完成的操作');
    expect(composer).toHaveValue('下一条消息的草稿');
    expect(screen.getByLabelText('移除 next-draft.png')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '核实上次发送' }));
    const requests = transport.requests.filter(({ request }) => request.pathId === 'agent.room.message');
    expect(requests).toHaveLength(2);
    expect(requests[1].request).toEqual(first);
    expect(composer).toHaveValue('下一条消息的草稿');
    expect(screen.getByLabelText('移除 next-draft.png')).toBeInTheDocument();
  });

  it('keeps the original admission identity if local projection fails after a successful Room ACK', async () => {
    const user = userEvent.setup();
    const { transport } = renderRoom(900, vi.fn(), undefined, undefined, undefined, undefined, vi.fn(),
      { ok: true, accepted: true });
    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    await user.type(composer, '已接收的请求');
    const accept = vi.spyOn(useRoomLiveStore.getState(), 'acceptMessage').mockImplementationOnce(() => {
      throw new Error('local projection unavailable');
    });
    try {
      await user.click(screen.getByRole('button', { name: '发送消息' }));
      await user.click(await screen.findByRole('button', { name: '核实上次发送' }));
      const requests = transport.requests.filter(({ request }) => request.pathId === 'agent.room.message');
      expect(requests).toHaveLength(2);
      expect(requests[1].request).toEqual(requests[0].request);
    } finally { accept.mockRestore(); }
  });

  it('keeps an accepted command settled when its surface metadata callback fails', async () => {
    const source = previewRoomSnapshot('room-presentation-failure');
    const acceptedWork = { ...source.room.workItems[0], id: 'accepted-work-callback' };
    const { onRoomUpdated, transport } = renderRoom(900, vi.fn(), source.room as unknown as RoomSummary, source,
      undefined, undefined, vi.fn(), { ok: true, workItem: acceptedWork });
    const user = userEvent.setup();
    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    await user.type(composer, '这条请求已经接收');
    onRoomUpdated.mockImplementation((updated: RoomSummary) => {
      if (updated.workItems?.some(item => item.id === acceptedWork.id)) throw new Error('synthetic metadata render failure');
    });
    await user.click(screen.getByRole('button', { name: '发送消息' }));
    expect(await screen.findByText('消息已接收，但界面暂未更新，请重新同步。')).toBeInTheDocument();
    expect(composer).toHaveValue('');
    expect(screen.queryByRole('button', { name: '核实上次发送' })).not.toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.room.message')).toHaveLength(1);
  });

  it('binds a normal Room message to the active executable WorkItem', async () => {
    const user = userEvent.setup();
    const { room, transport } = renderRoom(900);
    const composer = await screen.findByRole('textbox', { name: '协作消息' });

    await user.type(composer, '继续执行当前任务');
    await user.click(screen.getByRole('button', { name: '发送消息' }));

    await waitFor(() => expect(transport.requests.some(({ request }) => (
      request.pathId === 'agent.room.message'
    ))).toBe(true));
    const request = transport.requests.find(({ request: item }) => item.pathId === 'agent.room.message')?.request;
    expect(request?.body).toMatchObject({
      message: '继续执行当前任务',
      workItemId: room.workItems?.find((item) => ['queued', 'active', 'review', 'blocked'].includes(item.state))?.id,
    });
  });

  it('auto-confirms a legacy pending Room response without rendering approval UI', async () => {
    const user = userEvent.setup();
    const source = previewRoomSnapshot('room-gate');
    const gateWork = source.room.workItems[0];
    const gateSnapshot = {
      ...source,
      room: { ...source.room, workItems: [{ ...gateWork, state: 'active' as const }] },
      events: [],
      firstSequence: 0,
      lastSequence: 0,
      resumeToken: '',
    };
    const room = gateSnapshot.room as unknown as RoomSummary;
    const { transport } = renderRoom(
      900,
      vi.fn(),
      room,
      gateSnapshot,
      undefined,
      undefined,
      vi.fn(),
      (request) => {
        if (request.pathId === 'agent.room.message') {
          return {
            ok: true,
            startConfirmation: {
              status: 'pending',
              gateId: 'room-gate:preview',
              objective: '先确认 Room 执行范围',
              workItemId: 'room-work:preview',
              clientMessageId: 'room-client:preview',
              rootId: 'room-gate:turn-start',
              confirmedAtMs: 0,
            },
          };
        }
        if (request.pathId === 'agent.room.startGate.confirm') {
          return {
            ok: true,
            accepted: true,
            phase: 'execution',
            roomId: room.id,
            roomTurnId: 'room-gate:turn-start',
            clientMessageId: 'room-client:preview',
            timelineEvents: [],
          };
        }
        return undefined;
      },
    );
    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    const image = new File(['png'], 'start-scope.png', { type: 'image/png' });
    fireEvent.paste(composer, {
      clipboardData: { files: [image], items: [], getData: () => '' },
    });
    expect(await screen.findByLabelText('移除 start-scope.png')).toBeInTheDocument();

    await user.type(composer, '先确认 Room 执行范围');
    await user.click(screen.getByRole('button', { name: '发送消息' }));
    await waitFor(() => expect(transport.requests.find(({ request }) => (
      request.pathId === 'agent.room.startGate.confirm'
    ))?.request).toMatchObject({
      params: { roomId: room.id },
      body: { gateId: 'room-gate:preview', decision: 'confirm' },
    }));
    expect(screen.queryByRole('alert', { name: 'Room 开始执行确认' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '暂不开始' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '确认并开始' })).not.toBeInTheDocument();
    expect(composer).toHaveValue('');
  });

  it('retains the explicit execution-record sheet and enters collaboration mode only on explicit request', async () => {
    const user = userEvent.setup();
    const openWindow = vi.fn();
    const setCollaborationFocusGroup = vi.fn();
    const { container, room } = renderRoom(900, openWindow, undefined, undefined, undefined, undefined, setCollaborationFocusGroup);
    await screen.findByRole('textbox', { name: '协作消息' });

    const primaryNavigation = screen.getByRole('navigation', { name: 'Room 工作台视图' });
    expect(within(primaryNavigation).getAllByRole('button')).toHaveLength(6);
    for (const label of ['执行记录', '消息流', '协同模式', '对话', '星空']) {
      expect(within(primaryNavigation).getByRole('button', { name: label })).toHaveAttribute('aria-label', label);
    }
    expect(within(primaryNavigation).getByRole('button', { name: '执行记录' })).toHaveAttribute('aria-pressed', 'true');
    expect(within(primaryNavigation).getByRole('button', { name: '协同模式' })).toHaveAttribute('aria-pressed', 'false');
    expect(within(primaryNavigation).getByRole('button', { name: '对话' })).toHaveAttribute('aria-pressed', 'false');
    expect(within(primaryNavigation).getByRole('button', { name: '星空' })).toHaveAttribute('aria-pressed', 'false');
    expect(container.querySelector('.paw-room-workspace')).toHaveAttribute('data-panel', 'none');
    expect(container.querySelector('.paw-room-workspace')).toHaveAttribute('data-view', 'rounds');

    const rounds = screen.getByRole('region', { name: 'Room 行星任务表' });
    expect(within(rounds).getByText('并行实现 Room 任务图与依赖数据，整合后交给独立伙伴复核。')).toBeInTheDocument();
    expect(within(rounds).queryByRole('table')).not.toBeInTheDocument();
    expect(within(rounds).getByRole('region', { name: 'Earth 最终结果' })).toBeInTheDocument();
    expect(within(rounds).getByRole('region', { name: 'Mars 伙伴结果' })).toBeInTheDocument();
    expect(within(rounds).queryByRole('region', { name: 'Venus 当前任务' })).not.toBeInTheDocument();
    await user.click(within(rounds).getByRole('button', { name: '查看协作过程' }));
    expect(within(rounds).getByRole('region', { name: 'Venus 当前任务' })).toBeInTheDocument();
    expect(screen.queryByRole('log', { name: 'Room 公开对话' })).not.toBeInTheDocument();
    expect(openWindow).not.toHaveBeenCalled();

    await user.click(within(primaryNavigation).getByRole('button', { name: '协同模式' }));
    expect(setCollaborationFocusGroup).toHaveBeenCalledWith(`room:${room.id}`);

    const tools = screen.getByRole('complementary', { name: 'Room 协作态势' });
    expect(within(tools).getByRole('tablist', { name: '协作工具视图' }).querySelectorAll('[role=tab]')).toHaveLength(2);
    expect(within(tools).getByRole('tab', { name: '态势' })).toHaveAttribute('aria-selected', 'true');
    if (screen.queryByRole('button', { name: '展开 Room 控件' })) fireEvent.click(screen.getByRole('button', { name: '展开 Room 控件' }));
    expect(screen.getByRole('region', { name: 'Room 当前协作' })).toHaveTextContent('任务图依赖验证');
    expect(within(tools).getByRole('group', { name: '任务与关系详情' })).toHaveTextContent('实现 Room 依赖数据投影');
    /* Entering after settlement still opens the current round's real results. */
    await waitFor(() => expect(openWindow).toHaveBeenCalledTimes(2));
    expect(openWindow.mock.calls.map(([request]) => request.target.id)).toEqual([
      'participant-present', 'participant-firstlight',
    ]);

    /* PF-CM-013/PF-CM-020：态势弹出是真实可达的协作窗口入口，指向 focus 面板。 */
    openWindow.mockClear();
    await user.click(within(tools).getByRole('button', { name: '在协作窗口中打开协作态势' }));
    expect(openWindow).toHaveBeenLastCalledWith(expect.objectContaining({
      appId: 'agent',
      target: expect.objectContaining({ kind: 'room', id: room.id, panel: 'focus' }),
    }));

    await user.click(within(tools).getByRole('button', { name: '关闭协作态势' }));

    expect(screen.getByRole('region', { name: /主 Room/ })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '协作消息' })).toBeInTheDocument();
    expect(screen.queryByRole('complementary', { name: 'Room 协作态势' })).not.toBeInTheDocument();
    expect(container.querySelector('.paw-room-workspace')).toHaveAttribute('data-panel', 'none');
    expect(container.querySelector('.paw-room-workspace')).toHaveAttribute('data-view', 'rounds');
    expect(container.querySelector('.paw-room-workspace')).toHaveAttribute('data-collaboration-mode', 'true');
    expect(setCollaborationFocusGroup).toHaveBeenLastCalledWith(`room:${room.id}`);
    expect(within(primaryNavigation).getByRole('button', { name: '协同模式' })).toHaveAttribute('aria-pressed', 'true');
    expect(within(primaryNavigation).getByRole('button', { name: '协同模式' })).toHaveFocus();

    await user.click(within(primaryNavigation).getByRole('button', { name: '执行记录' }));
    expect(setCollaborationFocusGroup).toHaveBeenLastCalledWith(null);
    expect(container.querySelector('.paw-room-workspace')).toHaveAttribute('data-collaboration-mode', 'false');
    expect(within(primaryNavigation).getByRole('button', { name: '执行记录' })).toHaveAttribute('aria-pressed', 'true');

    /* Default conversation path pays nothing for the sky: no region, no
     * canvas, and the starfield module itself was never evaluated. */
    expect(screen.queryByRole('dialog', { name: 'Room 星空' })).not.toBeInTheDocument();
    expect(starfieldChunk.evaluated).toBe(false);
  });
  it('opens a participant observer from the main result while external Room focus owns the details', async () => {
    const user = userEvent.setup();
    const openWindow = vi.fn();
    const rendered = renderRoom(
      934,
      openWindow,
      undefined,
      undefined,
      undefined,
      undefined,
      vi.fn(),
      undefined,
      undefined,
      true,
      false,
      'session-window',
      'room:room-preview',
    );
    await screen.findByRole('textbox', { name: '协作消息' });
    await waitFor(() => expect(openWindow).toHaveBeenCalledTimes(2));
    expect(openWindow.mock.calls.every(([request]) => request.background === true)).toBe(true);
    openWindow.mockClear();

    expect(screen.queryByRole('complementary', { name: 'Room 协作态势' })).not.toBeInTheDocument();
    expect(rendered.container.querySelector('.paw-room-workspace')).toHaveAttribute('data-collaboration-mode', 'true');
    const rounds = screen.getByRole('region', { name: 'Room 行星任务表' });
    const mars = within(rounds).getByRole('region', { name: 'Mars 伙伴结果' });
    await user.click(within(mars).getByText('查看结果'));
    await user.click(within(mars).getByRole('button', { name: '查看 Mars 进展' }));

    expect(openWindow).toHaveBeenCalledWith(expect.objectContaining({
      background: false,
      target: expect.objectContaining({
        kind: 'participant',
        id: 'participant-firstlight',
      }),
    }));
  });

  it('closes only the inline panel when Escape is handled inside the aside', async () => {
    const user = userEvent.setup();
    const setCollaborationFocusGroup = vi.fn();
    const { container } = renderRoom(900, vi.fn(), undefined, undefined, undefined, undefined, setCollaborationFocusGroup);
    await screen.findByRole('textbox', { name: '协作消息' });
    await user.click(screen.getByRole('button', { name: '协同模式' }));
    const tools = screen.getByRole('complementary', { name: 'Room 协作态势' });

    fireEvent.keyDown(tools, { bubbles: true, key: 'Escape' });

    expect(screen.queryByRole('complementary', { name: 'Room 协作态势' })).not.toBeInTheDocument();
    expect(container.querySelector('.paw-room-workspace')).toHaveAttribute('data-collaboration-mode', 'true');
    expect(setCollaborationFocusGroup).toHaveBeenLastCalledWith('room:room-preview');
  });

  it.each([934, 1280])('keeps external Room focus compact with a stable draft across a rerender at %ipx', async (width) => {
    const user = userEvent.setup();
    const rendered = renderRoom(width, vi.fn(), undefined, undefined, undefined, undefined, vi.fn(), undefined, undefined, true, false, 'session-window', 'room:room-preview');
    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    await user.type(composer, '保留正在写的补充');
    const runtime = rendered.container.querySelector('.paw-room-workspace__runtime');
    // The preview Root is terminal; stale review items cannot resurrect it.
    await waitFor(() => expect(runtime).toHaveTextContent('本轮执行已结束'));
    expect(screen.queryByRole('navigation', { name: 'Room 工作台视图' })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Room 当前协作' })).not.toBeInTheDocument();
    expect(rendered.container.querySelector('.paw-window-title')).not.toBeInTheDocument();
    expect(rendered.container.querySelector('.paw-room-workspace__body')?.children).toHaveLength(1);

    rendered.setDesktopFocusGroup('room:room-preview');

    expect(screen.queryByRole('complementary', { name: 'Room 协作态势' })).not.toBeInTheDocument();
    expect(rendered.container.querySelector('.paw-room-workspace')).toHaveAttribute('data-panel', 'none');
    expect(rendered.container.querySelector('.paw-room-workspace')).toHaveAttribute('data-view', 'rounds');
    expect(screen.getByRole('textbox', { name: '协作消息' })).toBe(composer);
    expect(composer).toHaveValue('保留正在写的补充');
    expect(composer).toHaveFocus();
    expect(runtime).toHaveTextContent('本轮执行已结束');
  });

  it('replaces a stale embedded inspector on external focus and restores ordinary views after exit', async () => {
    const user = userEvent.setup();
    const rendered = renderRoom(934);
    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    await user.type(composer, '外部聚焦继续保留');
    await user.click(screen.getByRole('button', { name: '协同模式' }));
    const tools = screen.getByRole('complementary', { name: 'Room 协作态势' });
    const mars = within(tools).getByRole('button', { name: /^Mars，/ });
    await user.click(mars);
    expect(mars).toHaveAttribute('aria-pressed', 'true');

    rendered.setDesktopFocusGroup('room:room-preview');

    expect(screen.queryByRole('complementary', { name: 'Room 协作态势' })).not.toBeInTheDocument();
    expect(rendered.container.querySelector('.paw-room-workspace')).toHaveAttribute('data-panel', 'none');
    expect(rendered.container.querySelector('.paw-room-workspace__body')?.children).toHaveLength(1);
    expect(screen.queryByRole('navigation', { name: 'Room 工作台视图' })).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '协作消息' })).toBe(composer);
    expect(composer).toHaveValue('外部聚焦继续保留');

    rendered.setDesktopFocusGroup(null);

    expect(screen.queryByRole('complementary', { name: 'Room 协作态势' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '协同模式' })).toHaveAttribute('aria-pressed', 'false');
    expect(rendered.container.querySelector('.paw-room-workspace')).toHaveAttribute('data-collaboration-mode', 'false');
    const navigation = screen.getByRole('navigation', { name: 'Room 工作台视图' });
    expect(within(navigation).getAllByRole('button')).toHaveLength(6);
    expect(rendered.container.querySelector('.paw-window-title')).toHaveTextContent('Room 934');
    if (screen.queryByRole('button', { name: '展开 Room 控件' })) fireEvent.click(screen.getByRole('button', { name: '展开 Room 控件' }));
    expect(screen.getByRole('region', { name: 'Room 当前协作' })).toBeInTheDocument();
    await user.click(within(navigation).getByRole('button', { name: '对话' }));
    expect(rendered.container.querySelector('.paw-room-workspace')).toHaveAttribute('data-view', 'conversation');
    expect(within(navigation).getByRole('button', { name: '星空' })).toBeEnabled();

    rendered.setDesktopFocusGroup('room:room-preview');

    expect(rendered.container.querySelector('.paw-room-workspace')).toHaveAttribute('data-view', 'rounds');
    expect(screen.queryByRole('dialog', { name: 'Room 星空' })).not.toBeInTheDocument();
    expect(screen.queryByRole('complementary', { name: 'Room 协作态势' })).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '协作消息' })).toBe(composer);
    expect(composer).toHaveValue('外部聚焦继续保留');
  });

  it('opens the collaboration timeline as a Room view with every active planet as a lane', async () => {
    const user = userEvent.setup();
    const { container } = renderRoom(1200);
    await screen.findByRole('textbox', { name: '协作消息' });
    const navigation = screen.getByRole('navigation', { name: 'Room 工作台视图' });
    await user.click(within(navigation).getByRole('button', { name: '协作时间线' }));
    expect(container.querySelector('.paw-room-workspace')).toHaveAttribute('data-view', 'timeline');
    const stage = await screen.findByRole('region', { name: '多 Agent 协作时间线' });
    expect(within(stage).getByRole('button', { name: /^Earth，/ })).toBeInTheDocument();
    expect(within(stage).getByRole('button', { name: /^Mars，/ })).toBeInTheDocument();
    expect(within(stage).getByRole('button', { name: /^团队调度，/ })).toBeDisabled();
  });

  it('removes the omitted signal row from external focus geometry and keeps the runtime text readable', () => {
    expect(roomFocusCss).toMatch(/\.paw-room-workspace\[data-external-focus\]\[data-window-chrome='portal'\]\s*\{[^}]*grid-template-rows:\s*minmax\(0, 1fr\);/s);
    expect(roomFocusCss).toMatch(/\.paw-room-workspace\[data-external-focus\]\[data-window-chrome='fallback'\]\s*\{[^}]*grid-template-rows:\s*44px minmax\(0, 1fr\);/s);
    expect(roomFocusCss).toMatch(/\.paw-room-window-chrome\[data-external-focus\] \.paw-room-workspace__runtime > span\s*\{[^}]*font-size:\s*12px;/s);
  });

  it('moves collaboration tool focus and selection with horizontal tablist keys', async () => {
    const user = userEvent.setup();
    renderRoom(900);
    await screen.findByRole('textbox', { name: '协作消息' });
    await user.click(screen.getByRole('button', { name: '协同模式' }));

    const tablist = screen.getByRole('tablist', { name: '协作工具视图' });
    const focusTab = within(tablist).getByRole('tab', { name: '态势' });
    const governanceTab = within(tablist).getByRole('tab', { name: '治理' });
    focusTab.focus();

    fireEvent.keyDown(focusTab, { key: 'ArrowRight' });
    expect(governanceTab).toHaveFocus();
    expect(governanceTab).toHaveAttribute('aria-selected', 'true');
    expect(focusTab).toHaveAttribute('aria-selected', 'false');
    expect(governanceTab).toHaveAttribute('tabindex', '0');
    expect(focusTab).toHaveAttribute('tabindex', '-1');

    fireEvent.keyDown(governanceTab, { key: 'Home' });
    expect(focusTab).toHaveFocus();
    expect(focusTab).toHaveAttribute('aria-selected', 'true');

    fireEvent.keyDown(focusTab, { key: 'End' });
    expect(governanceTab).toHaveFocus();
    expect(governanceTab).toHaveAttribute('aria-selected', 'true');

    fireEvent.keyDown(governanceTab, { key: 'ArrowLeft' });
    expect(focusTab).toHaveFocus();
    expect(focusTab).toHaveAttribute('aria-selected', 'true');
  });

  it('opens the current running partners when collaboration mode is requested, leaving idle members closed', async () => {
    const user = userEvent.setup();
    const openWindow = vi.fn();
    /* A distinct Room id keeps this running snapshot independent from the
       terminal preview Room already replayed by the preceding test. */
    const completed = previewRoomSnapshot('room-running-collaboration');
    /* The third reviewer is still idle and has no admitted execution. */
    const events = completed.events.slice(0, 6);
    const running = {
      ...completed,
      room: {
        ...completed.room,
        lastEventSequence: events.length,
      },
      events,
      lastSequence: events.length,
      resumeToken: `room-running-collaboration:${events.length}`,
    };
    const view = renderRoom(
      900,
      openWindow,
      running.room as unknown as RoomSummary,
      running,
    );
    await screen.findByRole('textbox', { name: '协作消息' });
    expect(openWindow).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: '协同模式' }));

    await waitFor(() => expect(openWindow).toHaveBeenCalledTimes(2));
    expect(openWindow.mock.calls.map(([request]) => request)).toEqual([
      expect.objectContaining({
        background: true,
        target: expect.objectContaining({ id: 'participant-present', title: 'Earth' }),
      }),
      expect.objectContaining({
        background: true,
        target: expect.objectContaining({ id: 'participant-firstlight', title: 'Mars' }),
      }),
    ]);
    const roomId = running.room.id;
    const projection = structuredClone(useRoomLiveStore.getState().projections[roomId]!);
    const root = projection.turnsById[projection.turnOrder.find((id) => projection.turnsById[id]?.status === 'running')!]!;
    root.participantIds.push('participant-future');
    act(() => useRoomLiveStore.setState((state) => ({ projections: { ...state.projections, [roomId]: projection } })));
    await waitFor(() => expect(openWindow).toHaveBeenCalledTimes(3));
    expect(openWindow.mock.calls[2]?.[0].target.id).toBe('participant-future');

    const progressed = structuredClone(projection);
    progressed.turnsById[root.id]!.updatedAtMs += 1;
    act(() => useRoomLiveStore.setState((state) => ({ projections: { ...state.projections, [roomId]: progressed } })));
    expect(openWindow).toHaveBeenCalledTimes(3);

    const completedProjection = structuredClone(progressed);
    completedProjection.turnsById[root.id]!.status = 'completed';
    act(() => useRoomLiveStore.setState((state) => ({ projections: { ...state.projections, [roomId]: completedProjection } })));
    expect(openWindow).toHaveBeenCalledTimes(3);
    expect(view.closeWindow).not.toHaveBeenCalled();

    // A new round reopens its participants even if their previous results
    // were collapsed; ordinary progress in the same round never does.
    const nextRound = structuredClone(completedProjection);
    const nextRootId = `${root.id}:next`;
    nextRound.turnOrder.push(nextRootId);
    nextRound.turnsById[nextRootId] = {
      ...root, id: nextRootId, rootId: nextRootId, status: 'running',
      messageIds: ['next-round-user'], activityIds: [],
    };
    act(() => useRoomLiveStore.setState((state) => ({ projections: { ...state.projections, [roomId]: nextRound } })));
    await waitFor(() => expect(openWindow).toHaveBeenCalledTimes(6));
    expect(openWindow.mock.calls.slice(3).every(([request]) => request.background === true)).toBe(true);
  });

  it('opens the canonical full Session when a standalone result planet is clicked in the ordinary Room', async () => {
    const user = userEvent.setup();
    const openWindow = vi.fn();
    renderRoom(900, openWindow);
    await screen.findByRole('textbox', { name: '协作消息' });

    const marsResult = screen.getByRole('region', { name: 'Mars 伙伴结果' });
    await user.click(within(marsResult).getByText('查看结果'));
    await user.click(within(marsResult).getByRole('button', { name: '打开 Mars Session' }));

    expect(openWindow).toHaveBeenCalledTimes(1);
    expect(openWindow).toHaveBeenLastCalledWith(expect.objectContaining({
      background: false,
      target: expect.objectContaining({
        kind: 'session',
        id: 'session-room-firstlight',
        title: 'Mars',
      }),
    }));
  });

  it('keeps participant process inspection inside an App-owned Room when requested', async () => {
    const user = userEvent.setup();
    const openWindow = vi.fn();
    renderRoom(
      900,
      openWindow,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      true,
      false,
      'room-transcript',
    );
    await screen.findByRole('textbox', { name: '协作消息' });

    const marsResult = screen.getByRole('region', { name: 'Mars 伙伴结果' });
    await user.click(within(marsResult).getByText('查看结果'));
    await user.click(within(marsResult).getByRole('button', { name: '在 Room 中查看 Mars 进展' }));

    expect(openWindow).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '对话' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByLabelText('Room 公开对话')).toBeInTheDocument();
  });

  it('resumes a blocked WorkItem through the active Root and keeps failure retryable', async () => {
    const user = userEvent.setup();
    const completed = previewRoomSnapshot('room-blocked-resume');
    const events = completed.events.slice(0, 6);
    const blocked = {
      ...(completed.room as unknown as RoomSummary).workItems?.[0],
      id: 'room-work:blocked',
      rootTurnId: 'room-blocked-resume:turn-1',
      state: 'blocked',
      blocker: { reason: 'Runtime 暂时不可用', nextStep: '恢复后重新分派' },
    };
    const runningRoom = {
      ...completed.room,
      lastEventSequence: events.length,
      workItems: [blocked],
    };
    const running = {
      ...completed,
      room: runningRoom,
      events,
      lastSequence: events.length,
      resumeToken: `room-blocked-resume:${events.length}`,
    };
    const { transport } = renderRoom(
      900,
      vi.fn(),
      runningRoom as unknown as RoomSummary,
      running as unknown as ReturnType<typeof previewRoomSnapshot>,
      undefined,
      { ...blocked, state: 'active' },
    );
    await screen.findByRole('textbox', { name: '协作消息' });

    /* The current owner is Venus even though Earth remains accountable and
       acts as the Root. Recovery belongs to the owner row; the Root actor is
       asserted separately on the command below. */
    const resume = screen.getByRole('button', { name: '恢复 Venus 并重新分派' });
    await user.click(resume);
    await waitFor(() => expect(transport.requests.some(({ request }) => (
      request.pathId === 'agent.room.workItem.resume'
      && request.params?.roomId === 'room-blocked-resume'
      && request.params?.workItemId === 'room-work:blocked'
      && (request.body as { actorParticipantId?: string }).actorParticipantId === 'participant-present'
    ))).toBe(true));
    expect(screen.queryByText('恢复失败')).not.toBeInTheDocument();
  });

  it('keeps opening later planets when one planet fails and retries that planet in place', async () => {
    const user = userEvent.setup();
    const openWindow = vi.fn()
      .mockImplementationOnce(() => { throw new Error('window unavailable'); })
      .mockImplementation(() => undefined);
    const completed = previewRoomSnapshot('room-collaboration-retry');
    const events = completed.events.slice(0, 6);
    const running = {
      ...completed,
      room: { ...completed.room, lastEventSequence: events.length },
      events,
      lastSequence: events.length,
      resumeToken: `room-collaboration-retry:${events.length}`,
    };
    renderRoom(
      900,
      openWindow,
      running.room as unknown as RoomSummary,
      running,
    );
    await screen.findByRole('textbox', { name: '协作消息' });

    await user.click(screen.getByRole('button', { name: '协同模式' }));

    await waitFor(() => expect(openWindow).toHaveBeenCalledTimes(2));
    expect(openWindow.mock.calls[0]?.[0]).toMatchObject({
      background: true,
      target: { id: 'participant-present', title: 'Earth' },
    });
    expect(openWindow.mock.calls[1]?.[0]).toMatchObject({
      background: true,
      target: { id: 'participant-firstlight', title: 'Mars' },
    });
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('1 颗活跃行星未能打开');
    expect(within(alert).getByRole('button', { name: '重试打开 Earth' })).toBeInTheDocument();
    expect(within(alert).getByRole('button', { name: '交给 Trace Agent' })).toBeInTheDocument();

    await user.click(within(alert).getByRole('button', { name: '重试打开 Earth' }));

    expect(openWindow).toHaveBeenCalledTimes(3);
    expect(openWindow.mock.calls[2]?.[0]).toMatchObject({
      background: true,
      target: { id: 'participant-present', title: 'Earth' },
    });
    expect(screen.queryByText('1 颗活跃行星未能打开')).not.toBeInTheDocument();
  });

  it('turns the whole Room into one clickable solar system in 星空 mode', async () => {
    const user = userEvent.setup();
    const openWindow = vi.fn();
    const { container } = renderRoom(900, openWindow);
    const composer = await screen.findByRole('textbox', { name: '协作消息' });

    // Before the explicit 星空 click nothing starfield exists — neither the
    // region nor the module (the chunk stays un-fetched in production).
    expect(screen.queryByRole('dialog', { name: 'Room 星空' })).not.toBeInTheDocument();
    expect(starfieldChunk.evaluated).toBe(false);

    await user.click(screen.getByRole('button', { name: '星空' }));

    expect(container.querySelector('.paw-room-workspace')).toHaveAttribute('data-view', 'starfield');
    // The sky is an immersive fullscreen overlay portaled to <body>; it
    // resolves through the lazy boundary, so the lookup awaits the chunk.
    const sky = await screen.findByRole('dialog', { name: 'Room 星空' });
    expect(starfieldChunk.evaluated).toBe(true);
    expect(sky).toHaveAttribute('data-immersive');
    expect(within(sky).getByText('Sol')).toBeInTheDocument();
    // Preserve the conversation while removing its covered controls from navigation.
    expect(composer).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: '协作消息' })).not.toBeInTheDocument();

    // Picking a planet opens its detail card; opening the partner window is
    // an explicit second action, so a stray click never steals the stage.
    await user.click(within(sky).getByRole('button', { name: /^Mars，/ }));
    const card = within(sky).getByRole('complementary', { name: '天体详情' });
    await user.click(within(card).getByRole('button', { name: '打开伙伴窗口' }));

    const foregroundCalls = openWindow.mock.calls.filter(([request]) => request.background === false);
    expect(foregroundCalls).toHaveLength(1);
    expect(foregroundCalls[0]?.[0]).toMatchObject({
      target: expect.objectContaining({
        kind: 'participant',
        id: 'participant-firstlight',
        title: 'Mars',
      }),
    });

    // The exit control returns to the conversation view and tears the whole
    // stage down: no region, no leftover sky DOM, nothing left animating.
    await user.click(within(sky).getByRole('button', { name: /返回 Room/ }));
    expect(screen.queryByRole('dialog', { name: 'Room 星空' })).not.toBeInTheDocument();
    expect(document.querySelector('.paw-sf')).toBeNull();
    expect(document.querySelector('.paw-sf__canvas')).toBeNull();
    expect(container.querySelector('.paw-room-workspace')).toHaveAttribute('data-view', 'conversation');
  });

  it('leaves desktop collaboration focus when Room switches to any local view', async () => {
    const user = userEvent.setup();
    const setCollaborationFocusGroup = vi.fn();
    const rendered = renderRoom(901, vi.fn(), undefined, undefined, undefined, undefined, setCollaborationFocusGroup);
    const { room } = rendered;
    await screen.findByRole('textbox', { name: '协作消息' });
    const primaryNavigation = screen.getByRole('navigation', { name: 'Room 工作台视图' });

    for (const view of ['执行记录', '消息流', '对话', '星空'] as const) {
      await user.click(within(primaryNavigation).getByRole('button', { name: '协同模式' }));
      await waitFor(() => expect(setCollaborationFocusGroup).toHaveBeenLastCalledWith(`room:${room.id}`));
      await user.click(within(primaryNavigation).getByRole('button', { name: view }));
      expect(setCollaborationFocusGroup).toHaveBeenLastCalledWith(null);
      if (view === '星空') {
        rendered.unmount();
      }
    }
  });

  it('links a selected standalone result planet and its graph node while keeping collaboration open', async () => {
    const user = userEvent.setup();
    const openWindow = vi.fn();
    renderRoom(900, openWindow);
    await screen.findByRole('textbox', { name: '协作消息' });

    await user.click(screen.getByRole('button', { name: '协同模式' }));
    const tools = screen.getByRole('complementary', { name: 'Room 协作态势' });
    const marsResult = screen.getByRole('region', { name: 'Mars 伙伴结果' });
    await user.click(within(marsResult).getByText('查看结果'));
    await user.click(within(marsResult).getByRole('button', { name: '查看 Mars 进展' }));

    expect(marsResult).toHaveAttribute('data-selected', 'true');
    expect(within(tools).getByRole('button', { name: /^Mars，/ })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('complementary', { name: 'Room 协作态势' })).toBeInTheDocument();
    expect(openWindow).toHaveBeenCalledWith(expect.objectContaining({
      background: false,
      target: expect.objectContaining({ kind: 'participant', id: 'participant-firstlight', title: 'Mars' }),
    }));

    const mesh = within(tools).getByRole('group', { name: '任务与关系详情' });
    await user.click(within(mesh).getByRole('button', { name: /^Earth，/ }));
    const earthResult = screen.getByRole('region', { name: 'Earth 最终结果' });
    expect(earthResult).toHaveAttribute('data-selected', 'true');
    expect(within(mesh).getByRole('button', { name: /^Earth，/ })).toHaveAttribute('aria-pressed', 'true');
    expect(openWindow).toHaveBeenCalledWith(expect.objectContaining({
      background: false,
      target: expect.objectContaining({ kind: 'participant', id: 'participant-present', title: 'Earth' }),
    }));

    expect(screen.queryByRole('button', { name: /铺开 .* 位/ })).not.toBeInTheDocument();
    expect(screen.getByRole('complementary', { name: 'Room 协作态势' })).toBeInTheDocument();
    /* 自动展开只允许 background 调用；前台调用只来自用户选择的行星。 */
    const foregroundCalls = openWindow.mock.calls.filter(([request]) => request.background === false);
    expect(foregroundCalls).toHaveLength(2);
    expect(foregroundCalls[1]?.[0]).toMatchObject({
      target: expect.objectContaining({
        id: 'participant-present',
        title: 'Earth',
        subtitle: expect.not.stringContaining('Agent 2'),
      }),
    });
  });
  it('opens the tools panel on the same edge as the control that opens it', async () => {
    /* 按钮在右、面板在左 was the complaint: the 协作态势 control portals into
       the titlebar's trailing chrome slot, so the aside it opens has to land
       on the trailing edge too — declared, not left to DOM order. */
    const user = userEvent.setup();
    const { container } = renderRoom(900);
    await screen.findByRole('textbox', { name: '协作消息' });

    const chromeSlot = container.querySelector('.paw-window-titlebar > .paw-window-chrome-slot')!;
    expect(chromeSlot).not.toBeNull();
    expect(chromeSlot.querySelector('.paw-room-window-chrome')).not.toBeNull();
    expect(container.querySelector('.paw-window-leading-slot .paw-room-window-chrome')).toBeNull();

    await user.click(screen.getByRole('button', { name: '协同模式' }));
    const tools = screen.getByRole('complementary', { name: 'Room 协作态势' });
    expect(tools).toHaveAttribute('data-side', 'trailing');
    const body = container.querySelector('.paw-room-workspace__body')!;
    expect(body.lastElementChild).toBe(tools);
    expect(body.firstElementChild).toHaveClass('paw-room-workspace__main');
  });

  it('names the Room origin Sol only while a connected coordinator hosts it', async () => {
    const hosted = renderRoom(900);
    await screen.findByRole('textbox', { name: '协作消息' });
    expect(screen.getByLabelText('Agent 中的 Sol 协作模式')).toBeInTheDocument();
    expect(hosted.container.querySelector('.paw-room-window-chrome'))
      .toHaveAttribute('data-coordinator', 'true');
    fireEvent.click(screen.getByRole('button', { name: '展开 Room 控件' }));
    expect(screen.getByLabelText('Sol 当前状态')).toBeInTheDocument();

    cleanup();

    const unhosted = previewRoomSnapshot('room-preview').room as unknown as RoomSummary;
    const demoted = {
      ...unhosted,
      participants: unhosted.participants.map((participant) => ({
        ...participant,
        collaborationRole: participant.collaborationRole === 'coordinator'
          ? 'implementer'
          : participant.collaborationRole,
      })),
    };
    const { container } = renderRoom(900, vi.fn(), demoted);
    await screen.findByRole('textbox', { name: '协作消息' });

    expect(screen.queryByLabelText('Agent 中的 Sol 协作模式')).not.toBeInTheDocument();
    expect(container.querySelector('.paw-room-window-chrome')).not.toHaveAttribute('data-coordinator');
    expect(screen.queryByLabelText('Sol 当前状态')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '展开 Room 控件' }));
    expect(screen.getByLabelText('主 Room 当前状态')).toBeInTheDocument();
  });

  it('governs the Room with the product picker and one vocabulary for every choice', async () => {
    const user = userEvent.setup();
    const { container, transport } = renderRoom(900);
    await screen.findByRole('textbox', { name: '协作消息' });

    await user.click(screen.getByRole('button', { name: '协同模式' }));
    const tools = screen.getByRole('complementary', { name: 'Room 协作态势' });
    await user.click(within(tools).getByRole('tab', { name: '治理' }));
    const governance = container.querySelector('.paw-room-governance') as HTMLElement;
    expect(governance).not.toBeNull();

    // Native dropdowns were the last previous-generation control left in the
    // Room: an OS-drawn popup opening over the PAWOS window.
    expect(governance.querySelector('select')).toBeNull();

    // Every picker draws from the same user-facing vocabulary; the member row
    // still exposes the longer responsibility description beside its picker.
    const roleRow = governance.querySelector('.paw-room-governance__members article') as HTMLElement;
    const memberName = within(roleRow).getByRole('combobox').getAttribute('aria-label')?.replace(' 的分工', '') ?? '';
    expect(within(roleRow).getByRole('combobox')).toHaveTextContent('最终汇合与回复');
    expect(roleRow.querySelector('small')).toBeNull();
    expect(memberName).not.toBe('');

    await user.click(within(roleRow).getByRole('combobox'));
    const listbox = await screen.findByRole('listbox');
    expect(within(listbox).getByRole('option', { name: '最终独立复核' })).toBeInTheDocument();
    expect(within(listbox).queryByRole('option', { name: '复核' })).toBeNull();

    await user.click(within(listbox).getByRole('option', { name: '最终独立复核' }));
    await waitFor(() => expect(transport.requests.some(({ request }) => (
      request.pathId === 'agent.room.participant.update'
      && (request.body as { collaborationRole?: string }).collaborationRole === 'reviewer'
    ))).toBe(true));
  });

  it('edits all three Room permission layers without falling back to the legacy projection', async () => {
    const user = userEvent.setup();
    const { container, transport } = renderRoom(900);
    await screen.findByRole('textbox', { name: '协作消息' });

    await user.click(screen.getByRole('button', { name: '协同模式' }));
    const tools = screen.getByRole('complementary', { name: 'Room 协作态势' });
    await user.click(within(tools).getByRole('tab', { name: '治理' }));
    const governance = container.querySelector('.paw-room-governance') as HTMLElement;
    const roomPermission = within(governance).getByRole('combobox', {
      name: 'Room 边界配置模式',
    });
    const partnerPermission = within(governance).getByRole('combobox', {
      name: '行星 / Partner配置模式',
    });
    const toolAgentPermission = within(governance).getByRole('combobox', {
      name: '卫星 / Tool Agent配置模式',
    });

    expect(governance.querySelector('select')).toBeNull();
    expect(roomPermission).toHaveTextContent('全自动');
    expect(partnerPermission).toHaveTextContent('继承（Inherit）');
    expect(toolAgentPermission).toHaveTextContent('继承（Inherit）');
    expect(governance).toHaveTextContent('整个系统（/）；所选项目只提供上下文');
    expect(governance).toHaveTextContent('继承 Room 边界');

    await user.click(partnerPermission);
    let listbox = await screen.findByRole('listbox');
    await user.click(within(listbox).getByRole('option', { name: '全权限' }));
    await user.click(toolAgentPermission);
    listbox = await screen.findByRole('listbox');
    expect(within(listbox).getByRole('option', { name: '全自动' })).toHaveAttribute(
      'aria-disabled',
      'true',
    );
    await user.click(within(listbox).getByRole('option', { name: '只读（沙箱）' }));

    expect(governance).toHaveTextContent('未继承，直接配置');
    await user.click(within(governance).getByRole('button', { name: '保存' }));
    await waitFor(() => expect(
      transport.requests.some(({ request }) => request.pathId === 'agent.room.archive'),
    ).toBe(true));
    const request = transport.requests.find(
      ({ request: item }) => item.pathId === 'agent.room.archive',
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
    expect(request?.body).not.toHaveProperty('workspaceScopeConfirmation');
  });

  it('keeps a legacy Room permission policy visibly unavailable and omits guessed authority', async () => {
    const user = userEvent.setup();
    const previewRoom = previewRoomSnapshot('room-legacy-policy').room;
    const legacyRoom = {
      ...previewRoom,
      executionMode: 'full_trust',
      permissionPolicy: undefined,
    } as unknown as RoomSummary;
    const { container, transport } = renderRoom(900, vi.fn(), legacyRoom);
    await screen.findByRole('textbox', { name: '协作消息' });

    await user.click(screen.getByRole('button', { name: '协同模式' }));
    await user.click(within(
      screen.getByRole('complementary', { name: 'Room 协作态势' }),
    ).getByRole('tab', { name: '治理' }));
    const governance = container.querySelector('.paw-room-governance') as HTMLElement;
    expect(within(governance).getByLabelText('Room 分层权限不可用')).toHaveTextContent(
      '界面不会猜测或补成全权限',
    );
    expect(within(governance).queryByRole('combobox', {
      name: 'Room 边界配置模式',
    })).not.toBeInTheDocument();

    await user.click(within(governance).getByRole('button', { name: '保存' }));
    await waitFor(() => expect(
      transport.requests.some(({ request }) => request.pathId === 'agent.room.archive'),
    ).toBe(true));
    const request = transport.requests.find(
      ({ request: item }) => item.pathId === 'agent.room.archive',
    )?.request;
    expect(request?.body).not.toHaveProperty('permissionPolicy');
    expect(request?.body).not.toHaveProperty('executionMode');
  });

  it('keeps an inactive but visible Room window on the authoritative stream', async () => {
    const { controlTransport, transport } = renderRoom(
      900,
      vi.fn(),
      undefined,
      undefined,
      undefined,
      undefined,
      vi.fn(),
      undefined,
      undefined,
      false,
    );

    await waitFor(() => {
      expect(transport.requests.filter(
        ({ request }) => request.pathId === 'agent.room.conversationSnapshot',
      )).toHaveLength(1);
      expect(controlTransport.activeSubscriptionCount()).toBe(1);
    });
    expect(transport.requests.filter(
      ({ request }) => request.pathId === 'agent.session.backgroundJobs.list',
    )).toHaveLength(0);
  });

  it('does not initialize a Room surface while the document is hidden', async () => {
    setDocumentVisibility('hidden');
    try {
      const { transport } = renderRoom(900);
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(transport.requests.filter(({ request }) => request.pathId === 'agent.room.snapshot')).toHaveLength(0);
      expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.backgroundJobs.list')).toHaveLength(0);
    } finally {
      setDocumentVisibility('visible');
    }
  });
});

function renderRoom(
  width: number,
  openWindow = vi.fn(),
  record?: RoomSummary,
  snapshotOverride?: ReturnType<typeof previewRoomSnapshot>,
  initialDraft?: string,
  resumeResponse?: Record<string, unknown>,
  setCollaborationFocusGroup = vi.fn(),
  messageResponse?: Record<string, unknown> | ((request: ControlRequest) => Record<string, unknown> | undefined | Promise<Record<string, unknown> | undefined>),
  initialError?: string,
  active = true,
  snapshotFailure = false,
  participantProcessLocation: 'session-window' | 'room-transcript' = 'session-window',
  collaborationFocusGroup?: string | null,
  initialView: 'rounds' | 'default' = 'rounds',
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const room = record ?? previewRoomSnapshot('room-preview').room as unknown as RoomSummary;
  const transport = createPreviewTransport();
  Object.defineProperty(transport, 'connectionIdentity', { value: `room-workspace-test:${crypto.randomUUID()}`, configurable: true });
  const viewKey = recoveryScope(transport, `room:${room.id}`) + ':view:v2';
  if (initialView === 'rounds' && !localStorage.getItem(viewKey)) localStorage.setItem(viewKey, 'rounds');
  if (initialView === 'default') { localStorage.removeItem(viewKey); localStorage.setItem(recoveryScope(transport, `room:${room.id}`) + ':view', 'rounds'); }
  const requests: { request: ControlRequest }[] = [];
  const send = transport.request.bind(transport);
  transport.request = async <Response = unknown>(request: ControlRequest): Promise<Response> => {
    requests.push({ request });
    if (snapshotFailure && request.pathId === 'agent.room.snapshot') {
      throw new Error('Room sync offline');
    }
    if (snapshotOverride && request.pathId === 'agent.room.snapshot') {
      return snapshotOverride as Response;
    }
    if (snapshotOverride && request.pathId === 'agent.room.get') {
      return { ok: true, room: snapshotOverride.room } as Response;
    }
    if (resumeResponse && request.pathId === 'agent.room.workItem.resume') {
      return { ok: true, workItem: resumeResponse } as Response;
    }
    if (typeof messageResponse === 'function') {
      const response = await messageResponse(request);
      if (response !== undefined) return response as Response;
    }
    if (messageResponse && request.pathId === 'agent.room.message') {
      return messageResponse as Response;
    }
    return send<Response>(request);
  };
  const focusState = { value: collaborationFocusGroup };
  const closeWindow = vi.fn();
  const onRoomUpdated = vi.fn();
  const renderSurface = () => (
    <QueryClientProvider client={queryClient}>
      <ControlTransportProvider transport={transport}>
        <PawOsDesktopProvider collaborationFocusGroup={focusState.value} closeWindow={closeWindow} openWindow={openWindow} setCollaborationFocusGroup={setCollaborationFocusGroup}>
          <TooltipProvider>
            <PawWindowFrame
              active
              appId="agent"
              bounds={{ x: 0, y: 0, width, height: 720 }}
              focusLocked={focusState.value === `room:${room.id}`}
              onBoundsCommit={() => undefined}
              onClose={() => undefined}
              onFocus={() => undefined}
              onMinimize={() => undefined}
              onToggleMaximize={() => undefined}
              title={`Room ${width}`}
              targetKind="room"
              windowChrome="room-workspace"
              windowId={`room-${width}`}
              zIndex={10}
            >
              <PawRoomWorkspace
                active={active}
                initialDraft={initialDraft}
                initialError={initialError}
                participantProcessLocation={participantProcessLocation}
                personas={[]}
                record={room}
                recordId={room.id}
                onRoomUpdated={onRoomUpdated}
              />
            </PawWindowFrame>
          </TooltipProvider>
        </PawOsDesktopProvider>
      </ControlTransportProvider>
    </QueryClientProvider>
  );
  const rendered = render(renderSurface());
  return {
    closeWindow,
    onRoomUpdated,
    transport: { requests },
    controlTransport: transport,
    ...rendered,
    remount: () => {
      rendered.rerender(<></>);
      rendered.rerender(renderSurface());
    },
    setDesktopFocusGroup: (next: string | null | undefined) => {
      focusState.value = next;
      rendered.rerender(renderSurface());
    },
    room,
  };
}

function setDocumentVisibility(state: 'hidden' | 'visible'): void {
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    value: state,
  });
  document.dispatchEvent(new Event('visibilitychange'));
}
