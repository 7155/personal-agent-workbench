import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import type { ControlTransport } from '@/platform/transport';
import { PawRoomCollaboration, type RoomCollaborationSection } from './PawRoomCollaboration';
import type { RoomFocusProjection } from './room-focus-projection';
import { collabDemoRoom } from '@/test/fixtures/collab-timeline';

const clients: QueryClient[] = [];
afterEach(() => { cleanup(); clients.splice(0).forEach(client => client.clear()); vi.useRealTimers(); });
const preview = collabDemoRoom();
const focus: RoomFocusProjection = {
  goal: { title: '真实通信', description: '', rootId: 'room-demo:root-1', state: 'running' },
  workItems: [], handoffs: [], flow: [], rootEvidence: [], counts: { active: 0, review: 0, blocked: 0, completed: 0 },
  partners: [
    { participantId: 'p-earth', sessionId: 's-earth', ordinal: 0, celestialName: 'Earth', displayName: 'Earth', collaborationRole: 'coordinator', state: 'running', currentAction: '整合结果', ownedWorkItemIds: [], unread: false },
    { participantId: 'p-mars', sessionId: 's-mars', ordinal: 1, celestialName: 'Mars', displayName: 'Mars', state: 'running', currentAction: '核对结果', ownedWorkItemIds: [], unread: false },
  ],
};

function mount(request: ControlTransport['request'], active = true, projection = focus,
  initialSection: RoomCollaborationSection = 'tasks', onOpenParticipant?: (participantId: string) => void) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  clients.push(client);
  const transport = { request } as ControlTransport;
  const view = (currentFocus: RoomFocusProjection) => <QueryClientProvider client={client}><ControlTransportProvider transport={transport}>
    <PawRoomCollaboration focus={currentFocus} roomId={preview.room.id} room={preview.room} projection={preview.projection}
      active={active} initialSection={initialSection} onOpenParticipant={onOpenParticipant} />
  </ControlTransportProvider></QueryClientProvider>;
  const result = render(view(projection));
  return { ...result, client, updateFocus: (next: RoomFocusProjection) => result.rerender(view(next)) };
}

describe('live Room traffic reads', () => {
  it('reads a finished Room once instead of repeatedly fetching heavy retained trees', async () => {
    vi.useFakeTimers();
    const request = vi.fn(async (call) => call.pathId === 'agent.session.intercom.list' ? { ok: true, items: [] }
      : { ok: true, tree: { rootSessionId: call.query.sessionId, roots: [], nodeCount: 0 } });
    const terminal: RoomFocusProjection = { ...focus, goal: { ...focus.goal, state: 'completed' }, partners: focus.partners.map((partner) => ({ ...partner, state: 'completed' })) };
    mount(request as ControlTransport['request'], true, terminal);
    await act(async () => { await vi.advanceTimersByTimeAsync(20); });
    expect(request).toHaveBeenCalledTimes(3);
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(request).toHaveBeenCalledTimes(3);
  });

  it('uses one Room-wide queue read and one private-tree read per planet, keeping a failed read unknown', async () => {
    const request = vi.fn(async (call) => {
      if (call.pathId === 'agent.session.intercom.list') return { ok: true, items: [] };
      if (call.query?.sessionId === 's-mars') throw new Error('temporarily unavailable');
      return { ok: true, tree: { rootSessionId: 's-earth', roots: [], nodeCount: 0 } };
    });
    mount(request as ControlTransport['request']);
    const mesh = screen.getByRole('group', { name: '任务与关系详情' });
    await waitFor(() => expect(within(mesh).getByRole('button', { name: /^Earth，/ })).toHaveTextContent('卫星 0'));
    await waitFor(() => expect(within(mesh).getByRole('button', { name: /^Mars，/ })).toHaveTextContent('卫星暂不可用'));
    expect(request.mock.calls.filter(([call]) => call.pathId === 'agent.session.intercom.list')).toHaveLength(1);
    expect(request.mock.calls.filter(([call]) => call.pathId === 'agent.subagents.list')).toHaveLength(2);
    expect(request.mock.calls.every(([call]) => ['agent.session.intercom.list', 'agent.subagents.list'].includes(call.pathId))).toBe(true);
  });

  it('does not poll an inactive view or turn its missing data into zero', () => {
    const request = vi.fn();
    mount(request as ControlTransport['request'], false);
    expect(request).not.toHaveBeenCalled();
    expect(screen.getAllByText('卫星读取中')).toHaveLength(2);
    fireEvent.click(screen.getByRole('tab', { name: '消息往返' }));
    expect(screen.getByText('正在恢复 Room 的直接通信与消息记录…')).toBeInTheDocument();
    expect(screen.queryByText(/当前筛选没有可读取/)).not.toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
  });
});

const requestFlow: RoomFocusProjection = {
  ...focus,
  flow: [{ id: 'root-request', sourceParticipantId: 'root', targetParticipantIds: ['p-earth'], kind: 'request',
    summary: '确认共同目标', status: 'completed', createdAtMs: 10, sequence: 1, refs: [] }],
};

describe('Room collaboration origin identity', () => {
  it('names the shared main Room without inventing Sol when no connected coordinator hosts it', () => {
    const unhosted: RoomFocusProjection = { ...requestFlow, partners: requestFlow.partners.map(partner => (
      { ...partner, collaborationRole: 'implementer' }
    )) };
    mount(vi.fn(), false, unhosted, 'messages');
    const packets = screen.getByRole('list', { name: '往来事件' });
    expect(packets).toHaveTextContent('主 Room → Earth');
    expect(packets).not.toHaveTextContent('Sol → Earth');
    expect(screen.queryByRole('img', { name: /^Sol，/ })).not.toBeInTheDocument();
  });

  it('names Sol in the message origin when a connected coordinator takes the chair without adding a partner', () => {
    const unhosted: RoomFocusProjection = { ...requestFlow, partners: requestFlow.partners.map(partner => (
      { ...partner, collaborationRole: 'implementer' }
    )) };
    const page = mount(vi.fn(), false, unhosted, 'messages');
    expect(screen.getByRole('list', { name: '往来事件' })).toHaveTextContent('主 Room → Earth');
    page.updateFocus(requestFlow);
    expect(screen.getByRole('list', { name: '往来事件' })).toHaveTextContent('Sol → Earth');
    expect(screen.getByRole('button', { name: 'Sol' })).toBeDisabled();
    fireEvent.click(screen.getByRole('tab', { name: '任务与回执' }));
    const partners = screen.getByRole('group', { name: '任务与关系详情' });
    expect(within(partners).getAllByRole('button')).toHaveLength(2);
    expect(within(partners).queryByRole('button', { name: /^Sol，/ })).not.toBeInTheDocument();
  });

  it('drops Sol from the origin again when the only coordinator disconnects', () => {
    const page = mount(vi.fn(), false, requestFlow, 'messages');
    expect(screen.getByRole('list', { name: '往来事件' })).toHaveTextContent('Sol → Earth');
    page.updateFocus({ ...requestFlow, partners: requestFlow.partners.map(partner => partner.collaborationRole === 'coordinator'
      ? { ...partner, state: 'disconnected' } : partner) });
    expect(screen.getByRole('list', { name: '往来事件' })).toHaveTextContent('主 Room → Earth');
    expect(screen.queryByRole('button', { name: 'Sol' })).not.toBeInTheDocument();
  });
});

describe('unified Room collaboration sections', () => {
  it.each(['tasks', 'messages', 'timeline'] as const)('opens the requested %s section using canonical planet ordinals', (section) => {
    const traffic: RoomFocusProjection = { ...requestFlow, flow: [{ ...requestFlow.flow[0], sourceParticipantId: 'p-earth', targetParticipantIds: ['p-mars'] }] };
    const request = vi.fn();
    const page = mount(request, false, traffic, section);
    const labels = { tasks: '任务与回执', messages: '消息往返', timeline: '时间线' };
    expect(screen.getByRole('tab', { name: labels[section] })).toHaveAttribute('aria-selected', 'true');
    if (section === 'tasks') {
      const group = screen.getByRole('group', { name: '任务与关系详情' });
      for (const [name, ordinal] of [['Earth', '0'], ['Mars', '1']]) {
        expect(within(group).getByRole('button', { name: new RegExp(`^${name}，`) }).querySelector('[data-room-planet]')).toHaveAttribute('data-room-planet', ordinal);
      }
      expect(screen.queryByRole('region', { name: '往来记录' })).not.toBeInTheDocument();
    } else if (section === 'messages') {
      const route = screen.getByLabelText('选中消息的流转方向');
      expect(within(route).getByRole('button', { name: 'Earth' }).querySelector('[data-room-planet]')).toHaveAttribute('data-room-planet', '0');
      expect(within(route).getByRole('button', { name: 'Mars' }).querySelector('[data-room-planet]')).toHaveAttribute('data-room-planet', '1');
      expect(screen.queryByRole('region', { name: '任务与回执详情' })).not.toBeInTheDocument();
    } else {
      expect(screen.getByRole('button', { name: '从头回放' })).toBeInTheDocument();
      expect(Array.from(page.container.querySelectorAll('[data-room-planet]')).map(node => node.getAttribute('data-room-planet'))).toEqual(['0', '1', '2', '3']);
      expect(screen.queryByRole('region', { name: '任务与回执详情' })).not.toBeInTheDocument();
    }
    expect(request).not.toHaveBeenCalled();
  });

  it('switches sections by pointer and keyboard without dispatching, restarting a Session or duplicating the read owners', async () => {
    const request = vi.fn(async call => call.pathId === 'agent.session.intercom.list' ? { ok: true, items: [] }
      : { ok: true, tree: { rootSessionId: call.query.sessionId, roots: [], nodeCount: 0 } });
    const onOpenParticipant = vi.fn();
    mount(request as ControlTransport['request'], true, requestFlow, 'tasks', onOpenParticipant);
    await waitFor(() => expect(screen.getAllByText('卫星 0')).toHaveLength(2));
    expect(request).toHaveBeenCalledTimes(3);
    fireEvent.click(screen.getByRole('tab', { name: '消息往返' }));
    expect(screen.getByRole('list', { name: '往来事件' })).toHaveTextContent('Sol → Earth');
    fireEvent.click(screen.getByRole('tab', { name: '时间线' }));
    expect(screen.getByRole('button', { name: '从头回放' })).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole('tab', { name: '时间线' }), { key: 'End' });
    const tasks = screen.getByRole('tab', { name: '任务与回执' });
    expect(tasks).toHaveFocus();
    expect(tasks).toHaveAttribute('aria-selected', 'true');
    expect(request).toHaveBeenCalledTimes(3);
    expect(request.mock.calls.every(([call]) => ['agent.session.intercom.list', 'agent.subagents.list'].includes(call.pathId))).toBe(true);
    expect(onOpenParticipant).not.toHaveBeenCalled();
  });
});
