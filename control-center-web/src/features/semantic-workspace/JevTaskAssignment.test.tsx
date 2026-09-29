import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { previewRoomSnapshot } from '@/app/preview-room-data';
import type { RoomSummary } from '@/features/rooms/room-types';
import { JevTaskAssignment } from './JevTaskAssignment';
import { jevTaskStage, parseJevSnapshot } from './jev-execution';
import type { JevTaskControls } from './jev-task-assignment';

afterEach(cleanup);
const room = previewRoomSnapshot('assignment-room').room as unknown as RoomSummary;
const [owner, target] = room.participants;
const graph = parseJevSnapshot({ ok: true, mode: 'jev', graphId: 'graph', rootId: 'root', snapshotVersion: 'one', phase: 'execute', tasks: [
  { id: 'task', state: 'active', revision: 1, taskHash: 'exact', owner_id: owner.id, accepted_turn_id: 'dispatch' },
], effects: [{ effectId: 'dispatch', operation: 'dispatch', state: 'accepted', executionStatus: 'running', request: { taskId: 'task', taskRevision: 1 } }] }, 'graph');
const task = graph.tasks[0];
const controls = () => ({ load: vi.fn().mockResolvedValue({ graphId: 'graph', taskId: 'task', taskHash: 'exact', ownerId: owner.id,
  action: 'request_reclaim', targetParticipantIds: [target.id], unavailableReason: '' }), pending: vi.fn().mockReturnValue(undefined),
  submit: vi.fn().mockResolvedValue('requested') });

describe('task detail assignment controls', () => {
  it('loads candidates on demand and preserves the owner until a real assignment arrives', async () => {
    const api = controls(); const user = userEvent.setup();
    const view = render(<JevTaskAssignment graph={graph} task={task} room={room} controls={api} />);
    expect(api.load).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '更换伙伴' }));
    await user.selectOptions(await screen.findByRole('combobox', { name: '接手伙伴' }), target.id);
    expect(screen.getAllByRole('option')).toHaveLength(2);
    await user.click(screen.getByRole('button', { name: '回收并改派' }));
    expect(api.submit).toHaveBeenCalledWith({ action: 'request_reclaim', graphId: 'graph', taskId: 'task', taskHash: 'exact', targetParticipantId: target.id, reason: '用户在任务详情中调整分工' });
    expect(await screen.findByRole('status')).toHaveTextContent('等待原执行停止后交给 Mars');
    expect(screen.getByText('Earth', { selector: 'strong' })).toBeVisible();
    const reclaim = { reclaimId: 'reclaim', taskId: task.id, taskRevision: 1, dispatchId: 'dispatch', targetParticipantId: target.id, stage: 'awaiting_assignment' as const };
    view.rerender(<JevTaskAssignment graph={{ ...graph, reclaims: [reclaim] }} task={task} room={room} controls={api} />);
    expect(screen.getByRole('status')).toHaveTextContent('执行已停止，等待目标伙伴');
    expect(jevTaskStage(task, { ...graph, reclaims: [reclaim] })).toBe('reassigning');
    view.rerender(<JevTaskAssignment graph={{ ...graph, reclaims: [] }} task={{ ...task, ownerId: target.id, acceptedTurnId: '' }} room={room} controls={api} />);
    expect(screen.getByText('Mars', { selector: 'strong' })).toBeVisible();
    expect(screen.queryByText(/等待原执行停止/)).not.toBeInTheDocument();
  });
  it('explains fixed ownership and ignores a response for a changed task', async () => {
    const api = controls(); const user = userEvent.setup();
    api.load.mockResolvedValue({ graphId: 'graph', taskId: 'task', taskHash: 'exact', ownerId: owner.id, action: '', targetParticipantIds: [], unavailableReason: 'owner_locked' });
    const view = render(<JevTaskAssignment graph={graph} task={task} room={room} controls={api} />);
    await user.click(screen.getByRole('button', { name: '更换伙伴' }));
    expect(await screen.findByText('当前方案已指定负责伙伴，需调整方案后更换。')).toBeVisible();
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    view.unmount();
    api.load.mockResolvedValue({ graphId: 'graph', taskId: 'task', taskHash: 'newer-task', ownerId: owner.id, action: 'request_reclaim', targetParticipantIds: [target.id], unavailableReason: '' });
    render(<JevTaskAssignment graph={graph} task={task} room={room} controls={api} />);
    await user.click(screen.getByRole('button', { name: '更换伙伴' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('任务状态已变化');
    expect(screen.queryByRole('button', { name: '回收并改派' })).not.toBeInTheDocument();
  });
  it('offers only the original uncertain operation and never retries on mounting', async () => {
    const input = { action: 'reassign' as const, graphId: 'graph', taskId: 'task', taskHash: 'prior', targetParticipantId: target.id, reason: 'original' };
    let pending = true;
    const api: JevTaskControls = { load: vi.fn(), pending: () => pending ? input : undefined, submit: vi.fn().mockImplementation(async () => { pending = false; return 'applied'; }) };
    render(<JevTaskAssignment graph={graph} task={task} room={room} controls={api} />);
    expect(api.submit).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: '更换伙伴' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: '核实同一次操作' }));
    expect(api.submit).toHaveBeenCalledWith(input);
    expect(await screen.findByRole('status')).toHaveTextContent('责任已改派');
  });
  it('blocks detached and stopped views, and does not use a prior revision reclaim as current', async () => {
    const api = controls(); const view = render(<JevTaskAssignment graph={graph} task={task} room={room} controls={api} active={false} />);
    expect(screen.getByRole('button', { name: '更换伙伴' })).toBeDisabled();
    view.rerender(<JevTaskAssignment graph={{ ...graph, stopped: true }} task={task} room={room} controls={api} />);
    expect(screen.queryByRole('button', { name: '更换伙伴' })).not.toBeInTheDocument();
    const reclaim = { reclaimId: 'old', taskId: task.id, taskRevision: 0, dispatchId: 'dispatch', targetParticipantId: target.id, stage: 'awaiting_stop' as const };
    expect(jevTaskStage(task, { ...graph, reclaims: [reclaim] })).toBe('running');
    expect(jevTaskStage(task, { ...graph, reclaims: [{ ...reclaim, taskRevision: 1 }] })).toBe('reclaiming');
    await waitFor(() => expect(api.load).not.toHaveBeenCalled());
  });
});
