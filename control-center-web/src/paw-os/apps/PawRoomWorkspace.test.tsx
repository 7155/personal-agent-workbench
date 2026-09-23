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
import type { ControlRequest } from '@/platform/transport';
import type { RoomSummary } from '@/features/rooms/room-types';
import { useRoomLiveStore } from '@/features/rooms/state/live-store';
import { PawWindowFrame } from '../shell/PawWindowLayer';
import { PawRoomWorkspace } from './PawRoomWorkspace';
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
  useRoomLiveStore.getState().reset();
});

describe('PAWOS Room collaboration tools', () => {
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
            commandReceipt: { state: 'failed', clientMessageId: 'busy-message', causeCode: 'ROOM_PARTICIPANT_BUSY' },
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

  it('uses one round sheet by default and enters collaboration mode only on explicit request', async () => {
    const user = userEvent.setup();
    const openWindow = vi.fn();
    const setCollaborationFocusGroup = vi.fn();
    const { container, room } = renderRoom(900, openWindow, undefined, undefined, undefined, undefined, setCollaborationFocusGroup);
    await screen.findByRole('textbox', { name: '协作消息' });

    const primaryNavigation = screen.getByRole('navigation', { name: 'Room 工作台视图' });
    expect(within(primaryNavigation).getAllByRole('button')).toHaveLength(5);
    for (const label of ['对话与结果', '消息流', '协同模式', '完整记录', '星空']) {
      expect(within(primaryNavigation).getByRole('button', { name: label })).toHaveAttribute('aria-label', label);
    }
    expect(within(primaryNavigation).getByRole('button', { name: '对话与结果' })).toHaveAttribute('aria-pressed', 'true');
    expect(within(primaryNavigation).getByRole('button', { name: '协同模式' })).toHaveAttribute('aria-pressed', 'false');
    expect(within(primaryNavigation).getByRole('button', { name: '完整记录' })).toHaveAttribute('aria-pressed', 'false');
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
    expect(within(tools).getAllByRole('tab')).toHaveLength(2);
    expect(within(tools).getByRole('tab', { name: '态势' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('region', { name: 'Room 当前协作' })).toHaveTextContent('任务图依赖验证');
    expect(within(tools).getByRole('group', { name: '协作网状图' })).toHaveTextContent('实现 Room 依赖数据投影');
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

    await user.click(within(primaryNavigation).getByRole('button', { name: '对话与结果' }));
    expect(setCollaborationFocusGroup).toHaveBeenLastCalledWith(null);
    expect(container.querySelector('.paw-room-workspace')).toHaveAttribute('data-collaboration-mode', 'false');
    expect(within(primaryNavigation).getByRole('button', { name: '对话与结果' })).toHaveAttribute('aria-pressed', 'true');

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
    await user.click(within(mars).getByRole('button', { name: '打开 Mars Session' }));

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
    await waitFor(() => expect(runtime).toHaveTextContent('1 个工作项等待复核'));
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
    expect(runtime).toHaveTextContent('1 个工作项等待复核');
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
    expect(within(navigation).getAllByRole('button')).toHaveLength(5);
    expect(rendered.container.querySelector('.paw-window-title')).toHaveTextContent('Room 934');
    expect(screen.getByRole('region', { name: 'Room 当前协作' })).toBeInTheDocument();
    await user.click(within(navigation).getByRole('button', { name: '完整记录' }));
    expect(rendered.container.querySelector('.paw-room-workspace')).toHaveAttribute('data-view', 'conversation');
    expect(within(navigation).getByRole('button', { name: '星空' })).toBeEnabled();

    rendered.setDesktopFocusGroup('room:room-preview');

    expect(rendered.container.querySelector('.paw-room-workspace')).toHaveAttribute('data-view', 'rounds');
    expect(screen.queryByRole('dialog', { name: 'Room 星空' })).not.toBeInTheDocument();
    expect(screen.queryByRole('complementary', { name: 'Room 协作态势' })).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '协作消息' })).toBe(composer);
    expect(composer).toHaveValue('外部聚焦继续保留');
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
    await user.click(within(marsResult).getByRole('button', { name: '打开 Mars Session' }));

    expect(openWindow).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '完整记录' })).toHaveAttribute('aria-pressed', 'true');
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

    for (const view of ['对话与结果', '消息流', '完整记录', '星空'] as const) {
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
    await user.click(within(marsResult).getByRole('button', { name: '打开 Mars Session' }));

    expect(marsResult).toHaveAttribute('data-selected', 'true');
    expect(within(tools).getByRole('button', { name: /^Mars，/ })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('complementary', { name: 'Room 协作态势' })).toBeInTheDocument();
    expect(openWindow).toHaveBeenCalledWith(expect.objectContaining({
      background: false,
      target: expect.objectContaining({ kind: 'participant', id: 'participant-firstlight', title: 'Mars' }),
    }));

    const mesh = within(tools).getByRole('group', { name: '协作网状图' });
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
  messageResponse?: Record<string, unknown> | ((request: ControlRequest) => Record<string, unknown> | undefined),
  initialError?: string,
  active = true,
  snapshotFailure = false,
  participantProcessLocation: 'session-window' | 'room-transcript' = 'session-window',
  collaborationFocusGroup?: string | null,
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const room = record ?? previewRoomSnapshot('room-preview').room as unknown as RoomSummary;
  const transport = createPreviewTransport();
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
      const response = messageResponse(request);
      if (response !== undefined) return response as Response;
    }
    if (messageResponse && request.pathId === 'agent.room.message') {
      return messageResponse as Response;
    }
    return send<Response>(request);
  };
  const focusState = { value: collaborationFocusGroup };
  const closeWindow = vi.fn();
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
                onRoomUpdated={vi.fn()}
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
    transport: { requests },
    controlTransport: transport,
    ...rendered,
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
