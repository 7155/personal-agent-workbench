import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import PawRoomFocusParticipants, { PawRoomFocusParticipantBar } from './PawRoomFocusParticipants';
import { ControlTransportProvider } from '@/app/control-transport';
import type { ControlTransport } from '@/platform/transport';
import { useRoomLiveStore } from '@/features/rooms/state/live-store';
import type { useRoomLiveSession } from '@/features/rooms/runtime/use-room-live-session';
import type { RoomFocusProjection } from './room-focus-projection';
import type { RoomSatelliteSnapshots } from './room-message-flow';

const live = vi.hoisted(() => ({ callbacks: undefined as Parameters<typeof useRoomLiveSession>[0] | undefined }));
vi.mock('@/features/rooms/runtime/use-room-live-session', () => ({
  useRoomLiveSession: (callbacks: Parameters<typeof useRoomLiveSession>[0]) => { live.callbacks = callbacks; return () => undefined; },
}));
afterEach(() => { cleanup(); useRoomLiveStore.getState().reset(); live.callbacks = undefined; });
const focus: RoomFocusProjection = {
  goal: { title: '检查插件', description: '', rootId: 'root-1', state: 'completed' },
  rootEvidence: [], handoffs: [], workItems: [], counts: { active: 0, review: 0, blocked: 0, completed: 0 },
  partners: [
    { participantId: 'earth', sessionId: 's-earth', displayName: '整合伙伴', celestialName: 'Earth', collaborationRole: 'coordinator', state: 'completed', currentAction: '整理报告', latestReceipt: '完整主持报告只属于主对话。', ownedWorkItemIds: [], unread: false },
    { participantId: 'mars', sessionId: 's-mars', displayName: '复核伙伴', celestialName: 'Mars', state: 'idle', currentAction: '等待任务', ownedWorkItemIds: [], unread: false },
    { participantId: 'venus', sessionId: 's-venus', displayName: '资料伙伴', celestialName: 'Venus', state: 'stopped', currentAction: '已停止', ownedWorkItemIds: [], unread: false },
  ],
  flow: [
    { id: 'question', sourceParticipantId: 'earth', targetParticipantIds: ['mars'], kind: 'question', summary: '请核对版本', status: 'replied', createdAtMs: 10, sequence: 1, refs: [] },
    { id: 'answer', sourceParticipantId: 'mars', targetParticipantIds: ['earth'], kind: 'answer', summary: '已核对完整版本记录', replyToPacketId: 'question', status: 'delivered', createdAtMs: 20, sequence: 2, refs: [] },
  ],
};
const satellites: RoomSatelliteSnapshots = {
  earth: { status: 'ready', satellites: [] },
  mars: { status: 'error', satellites: [] },
  venus: { status: 'loading', satellites: [] },
};

function mount(overrides: Partial<Parameters<typeof PawRoomFocusParticipantBar>[0]> = {}) {
  const props = { focus, satellitesByParticipant: satellites, onSelect: vi.fn(), onCloseInspector: vi.fn(), intercomStatus: 'ready' as const, ...overrides };
  return { ...render(<PawRoomFocusParticipantBar {...props} />), props };
}

describe('compact Room participants', () => {
  it('hydrates from the shared conversation snapshot and follows roster metadata without requiring a full snapshot', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    const request = vi.fn(async (call) => call.pathId === 'agent.subagents.list'
      ? { ok: true, tree: { rootSessionId: call.query.sessionId, roots: [], nodeCount: 0 } }
      : { ok: true, items: [] });
    render(<QueryClientProvider client={client}><ControlTransportProvider transport={{ request } as unknown as ControlTransport}>
      <PawRoomFocusParticipants roomId="room-1" windows={[]} onInspect={vi.fn()} onCloseInspector={vi.fn()} />
    </ControlTransportProvider></QueryClientProvider>);
    expect(screen.getByText('正在恢复伙伴状态…')).toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
    const room = {
      id: 'room-1', title: '检查插件', status: 'active', routingPolicy: 'moderator', moderatorParticipantId: 'earth',
      participants: focus.partners.slice(0, 2).map((partner, ordinal) => ({
        id: partner.participantId, sessionId: partner.sessionId, ordinal, status: 'active', displayName: partner.displayName,
        collaborationRole: partner.collaborationRole,
      })),
    };
    act(() => live.callbacks!.onSnapshot('room-1', { room } as Parameters<NonNullable<typeof live.callbacks>['onSnapshot']>[1]));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Earth，待命，卫星 0' })).toBeInTheDocument());
    expect(useRoomLiveStore.getState().snapshotsByRoomId['room-1']).toBeUndefined();
    act(() => live.callbacks!.onMetadata('room-1', { ok: true, room: { ...room, participants: room.participants.slice(0, 1) } }));
    expect(screen.queryByRole('button', { name: /^Mars，/ })).not.toBeInTheDocument();
    expect(request.mock.calls.every(([call]) => ['agent.subagents.list', 'agent.session.intercom.list'].includes(call.pathId))).toBe(true);
  });

  it('starts with one action per partner and a separate message route, without repeating reports or empty windows', () => {
    mount();
    const nav = screen.getByRole('navigation', { name: 'Room 伙伴' });
    expect(within(nav).getAllByRole('button')).toHaveLength(5);
    expect(within(nav).getByRole('button', { name: 'Earth，已完成，卫星 0' })).toHaveAttribute('aria-pressed', 'false');
    expect(within(nav).getByRole('button', { name: 'Mars，待命，卫星暂不可用' })).toBeInTheDocument();
    expect(within(nav).getByRole('button', { name: 'Venus，已停止，卫星读取中' })).toBeInTheDocument();
    expect(screen.queryByText('完整主持报告只属于主对话。')).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Room 消息流' })).not.toBeInTheDocument();
    expect(screen.queryByText('还没有消息或执行轨迹')).not.toBeInTheDocument();
  });

  it('reactivates the chosen partner through state updates without collapsing any window', async () => {
    const user = userEvent.setup();
    const view = mount();
    await user.click(screen.getByRole('button', { name: 'Mars，待命，卫星暂不可用' }));
    expect(view.props.onSelect).toHaveBeenCalledExactlyOnceWith('mars');
    const next = { ...focus, partners: focus.partners.map((partner) => partner.participantId === 'earth' ? { ...partner, state: 'running' as const } : partner) };
    view.rerender(<PawRoomFocusParticipantBar {...view.props} focus={next} selectedParticipantId="mars" />);
    const selected = screen.getByRole('button', { name: 'Mars，待命，卫星暂不可用' });
    expect(selected).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Earth，进行中，卫星 0' })).toHaveAttribute('aria-pressed', 'false');
    await user.click(selected);
    expect(view.props.onCloseInspector).not.toHaveBeenCalled();
    expect(view.props.onSelect).toHaveBeenCalledTimes(2);
    expect(view.props.onSelect).toHaveBeenLastCalledWith('mars');
  });

  it('opens full message content and reply navigation on demand, then returns keyboard focus on Escape', async () => {
    const user = userEvent.setup();
    const { props } = mount();
    const trigger = screen.getByRole('button', { name: '消息流' });
    await user.click(trigger);
    expect(props.onCloseInspector).toHaveBeenCalledOnce();
    const panel = screen.getByRole('region', { name: 'Room 消息流' });
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    await user.click(within(panel).getByRole('button', { name: /Mars → Earth.*已核对完整版本记录/ }));
    expect(within(panel).getByText('回复的消息')).toBeInTheDocument();
    await user.click(within(panel).getByRole('button', { name: 'Earth：请核对版本' }));
    expect(within(panel).getByText('已收到 1 条回复')).toBeInTheDocument();
    expect(within(panel).getByRole('button', { name: '查看 Mars 的回复' })).toBeInTheDocument();
    fireEvent.keyDown(panel, { key: 'Escape' });
    expect(screen.queryByRole('region', { name: 'Room 消息流' })).not.toBeInTheDocument();
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(trigger).toHaveFocus();
  });
});

describe('Room assignment map', () => {
  it('shows actual assignment and criteria, and opens the owner session', async () => {
    const user = userEvent.setup();
    const assigned: RoomFocusProjection = { ...focus, handoffs: [{ id: 'handoff-1', sourceParticipantId: 'earth', targetParticipantId: 'mars', task: '核对构建版本', state: 'completed', createdAtMs: 30 }], workItems: [{ id: 'task-1', source: 'work-item', objective: '核对构建版本', ownerParticipantId: 'mars', verifierParticipantId: 'earth', state: 'running', reviewRequired: true, acceptanceCriteria: ['安装版本与源码哈希一致'], expectedOutput: '验收记录', evidence: [], updatedAtMs: 30 }] };
    const view = mount({ focus: assigned });
    const graph = screen.getByRole('region', { name: 'Room 任务关系' });
    expect(screen.getByRole('button', { name: '任务关系' })).toHaveAttribute('aria-expanded', 'true');
    expect(within(graph).getByLabelText('任务分派图')).toHaveTextContent('核对构建版本');
    expect(within(graph).getByLabelText('分工详情')).toHaveTextContent('安装版本与源码哈希一致');
    expect(within(graph).getByLabelText('分工详情')).toHaveTextContent('复核Earth');
    await user.click(within(graph).getByRole('button', { name: '查看实际会话与调用' }));
    expect(view.props.onSelect).toHaveBeenCalledWith('mars');
    expect(screen.queryByRole('region', { name: 'Room 任务关系' })).not.toBeInTheDocument();
  });
  it('keeps failed relations separate and restores keyboard focus on escape', async () => {
    const user = userEvent.setup();
    mount({ focus: { ...focus, handoffs: [{ id: 'failed', sourceParticipantId: 'earth', targetParticipantId: 'mars', task: '失败交接', state: 'failed', createdAtMs: 40 }] } });
    const trigger = screen.getByRole('button', { name: '任务关系' });
    const graph = screen.getByRole('region', { name: 'Room 任务关系' });
    await user.click(within(graph).getByRole('button', { name: /协作往来/ }));
    expect(within(graph).getByLabelText('已确认协作关系')).not.toHaveTextContent('失败交接');
    await user.click(within(graph).getByRole('button', { name: /Earth → Mars.*交接/ }));
    expect(within(graph).getByLabelText('分工详情')).toHaveTextContent('失败交接');
    expect(within(graph).getByLabelText('分工详情')).toHaveTextContent('失败');
    await user.keyboard('{Escape}');
    expect(trigger).toHaveFocus();
  });

  it('shows offered and unassigned work without inventing owners, reviews or completed traffic', () => {
    mount({ focus: { ...focus, workItems: [{
      id: 'offered', source: 'work-item', objective: '等待接手的数据整理',
      offeredToParticipantId: 'mars', accountableParticipantId: 'earth',
      state: 'waiting', reviewRequired: true, acceptanceCriteria: [], evidence: [], updatedAtMs: 1,
    }, {
      id: 'unassigned', source: 'work-item', objective: '未分派的地图检查',
      state: 'waiting', reviewRequired: false, acceptanceCriteria: [], evidence: [], updatedAtMs: 2,
    }] } });
    const map = screen.getByLabelText('任务分派图');
    expect(map).toHaveTextContent('待接收');
    expect(map).toHaveTextContent('等待分派');
    expect(map).toHaveTextContent('待指定');
    expect(map).toHaveTextContent('未要求');
    expect(map.querySelectorAll('[data-pending]')).toHaveLength(1);
    expect(within(map).getAllByRole('button', { name: '未指定' })[0]).toBeDisabled();
  });

  it('keeps a dismissed graph closed across progress updates and lets the user reopen it', async () => {
    const user = userEvent.setup();
    const view = mount();
    await user.click(screen.getByRole('button', { name: '关闭任务关系' }));
    view.rerender(<PawRoomFocusParticipantBar {...view.props} focus={{ ...focus, goal: { ...focus.goal, state: 'running' } }} />);
    expect(screen.queryByRole('region', { name: 'Room 任务关系' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '任务关系' }));
    expect(screen.getByRole('region', { name: 'Room 任务关系' })).toBeInTheDocument();
  });

});
