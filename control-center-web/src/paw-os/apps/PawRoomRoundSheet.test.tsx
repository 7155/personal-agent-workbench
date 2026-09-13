import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  appendOptimisticRoomMessage,
  applyRoomSnapshot,
  createRoomProjection,
  reduceRoomEvent,
  type RoomActivityProjection,
  type RoomProjectionState,
} from '@/contracts/room-reducer';
import { parseRoomEvent } from '@/contracts/validators';
import { TooltipProvider } from '@/components/primitives';
import { PawOsDesktopProvider } from '@/features/paw-os/surface-context';
import type { RoomParticipant, RoomSummary, RoomWorkItem } from '@/features/rooms/room-types';
import { PawRoomRoundSheet } from './PawRoomRoundSheet';

afterEach(cleanup);

describe('PawRoomRoundSheet (UR-170/172)', () => {
  it('surfaces a stopped reason immediately and opens its evidence in place', async () => {
    const user = userEvent.setup();
    const projection = projectionWithProgress('工具连续失败，已停止本轮以避免继续空转');
    projection.turnsById['turn-1']!.status = 'failed';
    projection.activitiesById['activity-earth']!.status = 'failed';
    projection.activitiesById['activity-earth']!.payload = { ...projection.activitiesById['activity-earth']!.payload, task: '', reason: 'repeated_failure_signature' };
    const onOpenParticipant = vi.fn();
    render(<PawRoomRoundSheet onOpenParticipant={onOpenParticipant} projection={projection} room={roomWith([participant('participant-earth', 'session-earth', 0)])} />);
    expect(screen.getByText('工具连续失败，已停止本轮以避免继续空转')).toBeVisible();
    expect(screen.queryByText('repeated_failure_signature')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '查看协作过程' })).toHaveAttribute('aria-expanded', 'false');
    await user.click(screen.getByRole('button', { name: '查看原因' }));
    expect(screen.getByRole('region', { name: 'Earth 公开进展与证据' })).toBeVisible();
    expect(onOpenParticipant).not.toHaveBeenCalled();
  });

  it('opens a partner locally and removes folded controls from keyboard navigation', async () => {
    const user = userEvent.setup();
    const onOpenParticipant = vi.fn();
    const room = roomWith([participant('participant-earth', 'session-earth', 0)]);
    const projection = projectionWithProgress('正在核对');
    const { rerender } = render(<PawRoomRoundSheet onOpenParticipant={onOpenParticipant} projection={projection} room={room} />);
    await user.click(screen.getByRole('button', { name: '查看 Earth 的任务与进展' }));
    const detail = screen.getByRole('region', { name: 'Earth 公开进展与证据' });
    expect(detail).toBeVisible();
    expect(onOpenParticipant).not.toHaveBeenCalled();
    const completed = { ...projection, turnsById: { 'turn-1': { ...projection.turnsById['turn-1']!, status: 'completed' as const } } };
    rerender(<PawRoomRoundSheet onOpenParticipant={onOpenParticipant} projection={completed} room={room} />);
    expect(screen.getByRole('region', { name: 'Earth 公开进展与证据' })).toBe(detail);
    expect(detail).toBeVisible();
    await user.click(screen.getByRole('button', { name: '收起 Earth 详情' }));
    expect(screen.queryByRole('region', { name: 'Earth 公开进展与证据' })).not.toBeInTheDocument();
    expect(detail.closest('[inert]')).not.toBeNull();
    await user.click(screen.getByRole('button', { name: '打开 Earth Session' }));
    expect(onOpenParticipant).toHaveBeenCalledWith('participant-earth');
  });

  it('keeps reading position when another user round arrives and offers an explicit jump', async () => {
    const user = userEvent.setup();
    const room = roomWith([participant('participant-earth', 'session-earth', 0), participant('participant-mars', 'session-mars', 1)]);
    const { rerender } = render(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={projectionWithProgress('第一轮')} room={room} />);
    const scroller = screen.getByRole('region', { name: 'Room 行星任务表' });
    const scrollTo = vi.fn();
    Object.defineProperties(scroller, { scrollTo: { value: scrollTo }, scrollHeight: { value: 1600 }, clientHeight: { value: 500 } });
    await waitFor(() => expect(scrollTo).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole('button', { name: '查看 Earth 的任务与进展' }));
    const reading = screen.getByRole('region', { name: 'Earth 公开进展与证据' });
    fireEvent.scroll(scroller, { target: { scrollTop: 180 } });
    scrollTo.mockClear();
    rerender(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={projectionWithTwoRounds()} room={room} />);
    expect(screen.getByRole('button', { name: '有新一轮对话' })).toBeVisible();
    expect(scrollTo).not.toHaveBeenCalled();
    expect(reading).toBeVisible();
    expect(scroller.scrollTop).toBe(180);
    await user.click(screen.getByRole('button', { name: '有新一轮对话' }));
    await waitFor(() => expect(scrollTo).toHaveBeenCalledTimes(1));
    expect(document.activeElement).toHaveAttribute('data-round-id', 'turn-2');
    await user.click(screen.getByRole('button', { name: '查看第 1 轮：完成 Room 任务表' }));
    await waitFor(() => expect(document.activeElement).toHaveAttribute('data-round-id', 'turn-1'));
    expect(screen.getByRole('button', { name: '折叠本轮任务' })).toHaveAttribute('aria-expanded', 'true');
  });

  it('announces an observed partner wake without replaying a restored running snapshot', async () => {
    const room = roomWith([participant('participant-earth', 'session-earth', 0)]);
    const initial = projectionWithProgress('已经在执行');
    const { rerender } = render(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={initial} room={room} />);
    expect(screen.queryByText('Earth 正在接手')).not.toBeInTheDocument();
    const waiting = projectionWithProgress('等待继续');
    waiting.activitiesById['activity-earth']!.status = 'waiting';
    waiting.turnsById['turn-1']!.status = 'queued';
    rerender(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={waiting} room={room} />);
    rerender(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={projectionWithProgress('收到新任务，开始执行')} room={room} />);
    expect(await screen.findByText('Earth 正在接手')).toBeVisible();
    const notice = screen.getByText('Earth 正在接手').closest('[role="status"]');
    rerender(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={projectionWithProgress('进展原地更新')} room={room} />);
    expect(screen.getByText('Earth 正在接手').closest('[role="status"]')).toBe(notice);
    const completed = projectionWithProgress('已完成');
    completed.turnsById['turn-1']!.status = 'completed';
    rerender(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={completed} room={room} />);
    expect(screen.queryByText('Earth 正在接手')).not.toBeInTheDocument();
  });

  it('shows recent progress first and retains earlier events behind an explicit control', async () => {
    const user = userEvent.setup();
    const projection = projectionWithProgress('已有公开进展');
    const base = projection.activitiesById['activity-earth']!;
    projection.turnsById['turn-1']!.activityIds = [];
    projection.activitiesById = {};
    projection.activityOrder = [];
    for (let index = 1; index <= 10; index += 1) {
      const id = `progress-${index}`;
      projection.activitiesById[id] = { ...base, id, summary: `进展步骤 ${index}`, createdAtMs: 1000 * index, updatedAtMs: 1000 * index };
      projection.turnsById['turn-1']!.activityIds.push(id);
      projection.activityOrder.push(id);
    }
    render(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={projection} room={roomWith([participant('participant-earth', 'session-earth', 0)])} />);
    await user.click(screen.getByRole('button', { name: '展开 Earth 详情' }));
    const history = screen.getByRole('list', { name: 'Earth 最近进展，最新在前' });
    expect(within(history).getAllByRole('listitem')).toHaveLength(6);
    expect(within(history).getAllByRole('listitem')[0]).toHaveTextContent('进展步骤 10');
    expect(within(history).queryByText('进展步骤 1')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /查看更早的 \d+ 条进展/u }));
    expect(within(history).getByText('进展步骤 1')).toBeVisible();
    await user.click(screen.getByRole('button', { name: '只看最近 6 条进展' }));
    expect(within(history).getAllByRole('listitem')).toHaveLength(6);
  });

  it('folds a table partner in place without discarding the detail or opening a Session', async () => {
    const user = userEvent.setup();
    const room = roomWith([participant('participant-earth', 'session-earth', 0), participant('participant-mars', 'session-mars', 1), participant('participant-venus', 'session-venus', 2)]);
    room.workItems = [activeWorkItem('work-mars', 'participant-mars'), activeWorkItem('work-venus', 'participant-venus')];
    const projection = projectionWithProgress('协调进度');
    for (const name of ['mars', 'venus']) {
      projection.activitiesById[`activity-${name}`] = activityForParticipant(projection.activitiesById['activity-earth']!, `activity-${name}`, `participant-${name}`, `session-${name}`, `${name} 正在执行`);
      projection.activityOrder.push(`activity-${name}`);
      projection.turnsById['turn-1']!.activityIds.push(`activity-${name}`);
      projection.turnsById['turn-1']!.participantIds.push(`participant-${name}`);
    }
    const onOpenParticipant = vi.fn();
    render(<PawRoomRoundSheet onOpenParticipant={onOpenParticipant} projection={projection} room={room} />);
    expect(screen.getByRole('table')).toBeVisible();
    await user.click(screen.getByRole('button', { name: '查看 Mars 的任务与进展' }));
    const detail = screen.getByRole('region', { name: 'Mars 公开进展与证据' });
    await user.click(screen.getByRole('button', { name: '收起 Mars 详情' }));
    expect(detail).toBeInTheDocument();
    expect(detail.closest('[inert]')).not.toBeNull();
    expect(onOpenParticipant).not.toHaveBeenCalled();
  });

  it('keeps a real reasoning event in expandable progress instead of a coordinator report', async () => {
    const user = userEvent.setup();
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-mars', 'session-mars', 1),
    ]);
    room.workItems = [activeWorkItem('work-mars', 'participant-mars')];
    const projection = reduceRoomEvent(projectionWithProgress('正在执行当前任务'), parseRoomEvent({
      schemaVersion: 'rag-ime.agent-room-event.v1', eventId: 'reasoning-source', sequence: 1, resumeToken: 'room-a:1',
      roomId: 'room-a', turnId: 'turn-1', participantId: 'participant-earth', sourceSessionId: 'session-earth',
      eventType: 'participant_activity', createdAtMs: 6,
      payload: { rootId: 'turn-1', dispatchId: 'dispatch-earth', sourceEventType: 'reasoning_summary',
        source: 'provider_reasoning_summary', state: 'running', summary: 'Planning test seam without browser' },
    })).state;
    render(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={projection} room={room} />);
    expect(screen.queryByRole('region', { name: 'Earth 主控汇报' })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Earth 最终结果' })).not.toBeInTheDocument();
    const progress = screen.getByRole('region', { name: 'Earth 主控进展' });
    expect(within(progress).getByRole('status')).toHaveTextContent('进行中');
    expect(within(progress).queryByText('Planning test seam without browser')).not.toBeInTheDocument();
    await user.click(within(progress).getByText('查看工作过程'));
    expect(within(progress).getByText('Planning test seam without browser')).toBeVisible();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('does not promote a coordinator result post or completed WorkItem before this Room turn ends', () => {
    const room = roomWith([participant('participant-earth', 'session-earth', 0)]);
    room.workItems = [workItemForOwner('coordinator-work', 'participant-earth', '一个工作步骤已完成')];
    const projection = projectionWithProgress('主控仍在执行');
    projection.turnsById['turn-1']!.terminalParticipantIds = ['participant-earth'];
    appendCoordinatorPost(projection, 'result', '这是已发出的回复，Room 仍在运行。');
    render(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={projection} room={room} />);
    expect(screen.queryByRole('region', { name: 'Earth 最终结果' })).not.toBeInTheDocument();
    const reply = screen.getByRole('region', { name: 'Earth 主控回复' });
    expect(reply).toHaveTextContent('这是已发出的回复，Room 仍在运行。');
    expect(within(reply).getByRole('status')).toHaveTextContent('进行中');
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('keeps one unassigned planet out of the task table and preserves its Session actions', () => {
    const projection = projectionWithProgress('尚未分配');
    projection.turnsById['turn-1'] = {
      ...projection.turnsById['turn-1']!,
      activityIds: [],
      participantIds: [],
    };
    projection.activitiesById = {};
    projection.activityOrder = [];

    render(
      <PawRoomRoundSheet
        onOpenParticipant={vi.fn()}
        projection={projection}
        room={roomWith([participant('participant-earth', 'session-earth', 0)])}
      />,
    );

    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    const starter = screen.getByRole('region', { name: 'Earth 未分配' });
    expect(starter.closest('.paw-room-round')).toBeNull();
    expect(starter).toHaveTextContent('Grill Me');
    expect(within(starter).getByRole('button', { name: '打开 Earth Session' })).toBeInTheDocument();
  });

  it('presents one assigned planet as a standalone task instead of a multi-column table', () => {
    const projection = projectionWithProgress('正在核对唯一负责人的交付');

    render(
      <PawRoomRoundSheet
        onOpenParticipant={vi.fn()}
        projection={projection}
        room={roomWith([participant('participant-earth', 'session-earth', 0)])}
      />,
    );

    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    const task = screen.getByRole('region', { name: 'Earth 当前任务' });
    expect(task.closest('.paw-room-round')).toBeNull();
    expect(task).toHaveTextContent('正在核对唯一负责人的交付');
    expect(task).toHaveTextContent('进行中');
    expect(within(task).getByRole('button', { name: '打开 Earth Session' })).toBeInTheDocument();
  });

  it('retains a single worker full assignment behind an explicit disclosure without a table', async () => {
    const user = userEvent.setup();
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-venus', 'session-venus', 2),
    ]);
    room.workItems = [activeWorkItem('work-venus', 'participant-venus')];
    const projection = projectionWithProgress('主控正在跟进');
    const fullTask = `${'逐项核对当前任务、证据和失败分支。'.repeat(24)}\n\n任务全文结尾：必须保留这一条验收要求。`;
    const workerActivity = activityForParticipant(
      projection.activitiesById['activity-earth']!, 'activity-venus',
      'participant-venus', 'session-venus', 'Venus 正在核对证据',
    );
    workerActivity.payload.task = fullTask;
    projection.activitiesById['activity-venus'] = workerActivity;
    projection.activityOrder.push('activity-venus');
    projection.turnsById['turn-1']!.activityIds.push('activity-venus');
    projection.turnsById['turn-1']!.participantIds.push('participant-venus');

    render(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={projection} room={room} />);

    const task = screen.getByRole('region', { name: 'Venus 当前任务' });
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(task).toHaveTextContent('Venus 正在核对证据');
    expect(task).not.toHaveTextContent('任务全文结尾');
    await user.click(within(task).getByText('查看完整任务'));
    expect(within(task).getByText('任务全文结尾：必须保留这一条验收要求。')).toBeVisible();
    expect(within(task).getByRole('button', { name: '打开 Venus Session' })).toBeInTheDocument();
  });

  it('keeps the coordinator progress outside the worker table and discloses process text explicitly', async () => {
    const user = userEvent.setup();
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-mars', 'session-mars', 1),
      participant('participant-venus', 'session-venus', 2),
    ]);
    room.workItems = [
      activeWorkItem('work-mars', 'participant-mars'),
      activeWorkItem('work-venus', 'participant-venus'),
    ];
    const projection = projectionWithProgress('主控已经汇总当前公开进展');
    projection.turnsById['turn-1'] = {
      ...projection.turnsById['turn-1']!,
      participantIds: ['participant-earth', 'participant-mars', 'participant-venus'],
      activityIds: ['activity-earth', 'activity-mars', 'activity-venus'],
    };
    projection.activitiesById['activity-mars'] = activityForParticipant(
      projection.activitiesById['activity-earth']!,
      'activity-mars',
      'participant-mars',
      'session-mars',
      'Mars 正在执行自己的任务',
    );
    projection.activitiesById['activity-venus'] = activityForParticipant(
      projection.activitiesById['activity-earth']!,
      'activity-venus',
      'participant-venus',
      'session-venus',
      'Venus 正在执行自己的任务',
    );
    projection.activityOrder = ['activity-earth', 'activity-mars', 'activity-venus'];

    render(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={projection} room={room} />);

    const summary = screen.getByRole('region', { name: 'Earth 主控进展' });
    expect(summary.closest('.paw-room-round')).toBeNull();
    expect(summary).toHaveTextContent('主控正在执行当前任务，尚未发布面向你的回复。');
    expect(summary).not.toHaveTextContent('主控已经汇总当前公开进展');
    await user.click(within(summary).getByText('查看工作过程'));
    expect(summary).toHaveTextContent('主控已经汇总当前公开进展');
    const table = screen.getByRole('table', { name: '完成 Room 任务表 · 行星进展' });
    expect(within(table).getAllByRole('row')).toHaveLength(3);
    expect(within(table).queryByText('主控已经汇总当前公开进展')).not.toBeInTheDocument();
    expect(within(table).queryByText('Earth')).not.toBeInTheDocument();
    expect(screen.getByText('2 颗行星 · 协作中')).toBeInTheDocument();
  });

  it('does not create a one-row table when only one worker accompanies the coordinator', () => {
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-mars', 'session-mars', 1),
    ]);
    room.workItems = [activeWorkItem('work-mars', 'participant-mars')];
    const projection = projectionWithProgress('主控正在整理 Mars 的交付');
    projection.turnsById['turn-1'] = {
      ...projection.turnsById['turn-1']!,
      participantIds: ['participant-earth', 'participant-mars'],
      activityIds: ['activity-earth', 'activity-mars'],
    };
    projection.activitiesById['activity-mars'] = activityForParticipant(
      projection.activitiesById['activity-earth']!,
      'activity-mars',
      'participant-mars',
      'session-mars',
      'Mars 正在执行唯一工作',
    );
    projection.activityOrder = ['activity-earth', 'activity-mars'];

    render(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={projection} room={room} />);

    expect(screen.getByRole('region', { name: 'Earth 主控进展' })).toBeInTheDocument();
    const task = screen.getByRole('region', { name: 'Mars 当前任务' });
    expect(task).toHaveTextContent('Mars 正在执行唯一工作');
    expect(task.closest('.paw-room-round')).toBeNull();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('keeps a completed coordinator progress post as a normal report, not a submitted final', () => {
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-mars', 'session-mars', 1),
    ]);
    const projection = projectionWithProgress('主控正在等待伙伴交付');
    projection.turnsById['turn-1'] = {
      ...projection.turnsById['turn-1']!,
      participantIds: ['participant-earth', 'participant-mars'],
      activityIds: ['activity-earth', 'activity-mars'],
      terminalParticipantIds: ['participant-earth'],
    };
    projection.activitiesById['activity-earth'] = {
      ...projection.activitiesById['activity-earth']!,
      status: 'completed',
    };
    projection.activitiesById['activity-mars'] = activityForParticipant(
      projection.activitiesById['activity-earth']!,
      'activity-mars',
      'participant-mars',
      'session-mars',
      'Mars 仍在执行',
    );
    projection.activitiesById['activity-mars'] = {
      ...projection.activitiesById['activity-mars']!,
      status: 'running',
    };
    projection.activityOrder = ['activity-earth', 'activity-mars'];
    appendCoordinatorPost(projection, 'progress', '主控正在等待伙伴交付');

    render(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={projection} room={room} />);

    const summary = screen.getByRole('region', { name: 'Earth 主控回复' });
    expect(summary).toHaveTextContent('主控正在等待伙伴交付');
    expect(summary).not.toHaveTextContent('最终结果');
    expect(summary).not.toHaveTextContent('已提交');
    expect(screen.queryByRole('region', { name: 'Earth 最终结果' })).not.toBeInTheDocument();
  });

  it('renders the complete current streaming host report without promoting it to a final', () => {
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-mars', 'session-mars', 1),
    ]);
    room.workItems = [activeWorkItem('work-mars', 'participant-mars')];
    const projection = projectionWithProgress('主持正在整理');
    appendCoordinatorPost(projection, 'progress', `${'公开汇报内容。'.repeat(80)}\n\n流式正文末尾仍然可读。`);
    const message = Object.values(projection.messagesById).find((item) => item.role === 'assistant')!;
    message.status = 'streaming';

    render(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={projection} room={room} />);

    const report = screen.getByRole('region', { name: 'Earth 主控回复' });
    expect(report).toHaveTextContent('流式正文末尾仍然可读。');
    expect(within(report).getByRole('status')).toHaveTextContent('进行中');
    expect(screen.queryByRole('region', { name: 'Earth 最终结果' })).not.toBeInTheDocument();
  });

  it('keeps idle roster cards out of a streaming coordinator reply and counts only its active lane', () => {
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-mars', 'session-mars', 1),
      participant('participant-venus', 'session-venus', 2),
    ]);
    const projection = projectionWithProgress('主持正在回答');
    appendCoordinatorPost(projection, 'progress', '这是当前问题的回复。');
    const message = Object.values(projection.messagesById).find((item) => item.role === 'assistant')!;
    message.status = 'streaming';

    render(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={projection} room={room} />);

    const report = screen.getByRole('region', { name: 'Earth 主控回复' });
    expect(report).toHaveTextContent('这是当前问题的回复。');
    expect(within(report).getByRole('status')).toHaveTextContent('进行中');
    expect(screen.queryByRole('region', { name: 'Mars 未分配' })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Venus 未分配' })).not.toBeInTheDocument();
    expect(screen.getByText('1 颗行星 · 协作中')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '收起协作过程' })).not.toBeInTheDocument();
  });

  it('keeps the coordinator report visible and makes partner submissions expandable', async () => {
    const user = userEvent.setup();
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-mars', 'session-mars', 1),
      participant('participant-venus', 'session-venus', 2),
    ]);
    room.workItems = [
      workItemForOwner('work-mars-result', 'participant-mars', 'Mars 已提交结果。'),
      workItemForOwner('work-venus-result', 'participant-venus', 'Venus 已提交结果。'),
    ];
    const projection = projectionWithProgress('主控已汇总两位伙伴的最终结果');
    projection.turnsById['turn-1'] = {
      ...projection.turnsById['turn-1']!,
      status: 'completed',
      participantIds: ['participant-earth', 'participant-mars', 'participant-venus'],
      activityIds: ['activity-earth'],
      terminalParticipantIds: ['participant-earth'],
    };
    projection.activitiesById['activity-earth'] = {
      ...projection.activitiesById['activity-earth']!,
      status: 'completed',
    };
    appendCoordinatorPost(projection, 'progress', '主控已汇总两位伙伴的最终结果');

    render(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={projection} room={room} />);

    expect(screen.getByRole('region', { name: 'Earth 主控回复' })).toHaveTextContent(
      '主控已汇总两位伙伴的最终结果',
    );
    expect(screen.getAllByRole('region', { name: /伙伴结果$/u })).toHaveLength(2);
    const mars = screen.getByRole('region', { name: 'Mars 伙伴结果' });
    const disclosure = within(mars).getByRole('button', { name: /Mars.*查看结果/u });
    expect(disclosure).toHaveAttribute('aria-expanded', 'false');
    await user.click(within(mars).getByText('查看结果'));
    expect(disclosure).toHaveAttribute('aria-expanded', 'true');
    expect(within(mars).getByText('收起结果')).toBeVisible();
    expect(within(mars).getByText('Mars 已提交结果。')).toBeVisible();
    expect(within(mars).getByRole('button', { name: '打开 Mars Session' })).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('keeps a coordinator result post as a current reply while this Room turn and other planets are active', () => {
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-mars', 'session-mars', 1),
      participant('participant-venus', 'session-venus', 2),
    ]);
    room.workItems = [
      workItemForOwner('work-earth-summary', 'participant-earth', '主控汇报：已确认两位伙伴的当前进展。'),
      activeWorkItem('work-mars', 'participant-mars'),
      activeWorkItem('work-venus', 'participant-venus'),
    ];
    const projection = projectionWithProgress('主控汇报已提交，伙伴仍在执行');
    projection.turnsById['turn-1'] = {
      ...projection.turnsById['turn-1']!,
      participantIds: ['participant-earth', 'participant-mars', 'participant-venus'],
      activityIds: ['activity-earth', 'activity-mars', 'activity-venus'],
    };
    projection.activitiesById['activity-mars'] = activityForParticipant(
      projection.activitiesById['activity-earth']!,
      'activity-mars',
      'participant-mars',
      'session-mars',
      'Mars 正在执行',
    );
    projection.activitiesById['activity-venus'] = activityForParticipant(
      projection.activitiesById['activity-earth']!,
      'activity-venus',
      'participant-venus',
      'session-venus',
      'Venus 正在执行',
    );
    projection.activityOrder = ['activity-earth', 'activity-mars', 'activity-venus'];
    appendCoordinatorPost(
      projection,
      'result',
      '主控汇报：已确认两位伙伴的当前进展。',
    );

    render(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={projection} room={room} />);

    const result = screen.getByRole('region', { name: 'Earth 主控回复' });
    expect(result).toHaveTextContent('主控汇报：已确认两位伙伴的当前进展');
    expect(within(result).getByRole('status')).toHaveTextContent('进行中');
    expect(screen.queryByRole('region', { name: 'Earth 最终结果' })).not.toBeInTheDocument();
    const table = screen.getByRole('table', { name: '完成 Room 任务表 · 行星进展' });
    expect(table).not.toContainElement(result);
    expect(within(table).queryByText('Earth')).not.toBeInTheDocument();
    expect(within(table).getByText('Mars')).toBeInTheDocument();
    expect(within(table).getByText('Venus')).toBeInTheDocument();
    expect(screen.getByText('2 颗行星 · 协作中')).toBeInTheDocument();
    expect(screen.queryByText('3 颗行星 · 协作中')).not.toBeInTheDocument();
  });

  it('keeps a stopped host report readable while collapsing only the settled collaboration process', async () => {
    const user = userEvent.setup();
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-mars', 'session-mars', 1),
      participant('participant-venus', 'session-venus', 2),
    ]);
    const projection = projectionWithProgress('主持正在整理公开内容');
    const fullReport = `## 已完成的部分\n\n${'保留真实的工作记录。'.repeat(70)}\n\n## 下一步\n\n打开产物继续核对交互。`;
    appendCoordinatorPost(projection, 'progress', fullReport);
    projection.turnsById['turn-1'] = {
      ...projection.turnsById['turn-1']!,
      status: 'aborted',
      participantIds: ['participant-earth', 'participant-mars', 'participant-venus'],
      activityIds: ['activity-earth', 'activity-mars', 'activity-venus'],
    };
    projection.activitiesById['activity-earth'] = { ...projection.activitiesById['activity-earth']!, status: 'aborted' };
    for (const [name, id] of [['Mars', 'mars'], ['Venus', 'venus']]) {
      projection.activitiesById[`activity-${id}`] = activityForParticipant(
        projection.activitiesById['activity-earth']!, `activity-${id}`, `participant-${id}`, `session-${id}`, `${name} 保留当前进展`,
      );
    }
    projection.activityOrder = ['activity-earth', 'activity-mars', 'activity-venus'];

    render(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={projection} room={room} />);

    const report = screen.getByRole('region', { name: 'Earth 主控回复' });
    expect(report).toHaveTextContent('打开产物继续核对交互。');
    expect(within(report).getByRole('heading', { name: '下一步' })).toBeVisible();
    expect(within(report).getByRole('status')).toHaveTextContent('已停止');
    expect(report).not.toHaveAttribute('aria-live');
    expect(screen.queryByRole('region', { name: 'Earth 最终结果' })).not.toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '查看协作过程' }));
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(report).toBeVisible();
    await user.click(screen.getByRole('button', { name: '收起协作过程' }));
    expect(report).toBeVisible();
  });

  it('counts only planets assigned in this round when the Room roster has idle roles', () => {
    const projection = projectionWithProgress('只有 Earth 收到本轮任务');
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-mars', 'session-mars', 1),
      participant('participant-venus', 'session-venus', 2),
    ]);

    render(
      <PawRoomRoundSheet
        onOpenParticipant={vi.fn()}
        projection={projection}
        room={room}
      />,
    );

    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Earth 当前任务' })).toHaveTextContent(
      '只有 Earth 收到本轮任务',
    );
    expect(screen.getByText('1 颗行星 · 协作中')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Mars 当前任务' })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Venus 当前任务' })).not.toBeInTheDocument();
  });

  it('keeps one row mounted while progress updates and opens the real planet identity explicitly', async () => {
    const user = userEvent.setup();
    const onOpenParticipant = vi.fn();
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-mars', 'session-mars', 1),
    ]);
    const projection = projectionWithProgress('正在核对 dispatch 回执');
    projection.turnsById['turn-1'] = {
      ...projection.turnsById['turn-1']!,
      activityIds: ['activity-earth', 'activity-mars'],
      participantIds: ['participant-earth', 'participant-mars'],
    };
    projection.activitiesById['activity-mars'] = {
      ...projection.activitiesById['activity-earth']!,
      id: 'activity-mars',
      participantId: 'participant-mars',
      sourceSessionId: 'session-mars',
      summary: 'Mars 正在并行核对交付',
      payload: {
        ...projection.activitiesById['activity-earth']!.payload,
        dispatchId: 'dispatch-mars',
        task: '并行检查 Room 证据',
      },
    };
    projection.activityOrder = ['activity-earth', 'activity-mars'];
    const { container, rerender } = render(
      <PawRoomRoundSheet
        onOpenParticipant={onOpenParticipant}
        projection={projection}
        room={room}
      />,
    );

    const surface = screen.getByRole('region', { name: 'Room 行星任务表' });
    expect(within(surface).getByText('完成 Room 任务表')).toBeInTheDocument();
    /* Earth is the coordinator and Mars is the only worker. The coordinator
       synthesis stays a standalone card, so this one-worker round must not
       regress to a one-row table. */
    expect(within(surface).queryByRole('columnheader', { name: '行星' })).not.toBeInTheDocument();
    expect(within(surface).getByRole('region', { name: 'Earth 主控进展' })).toBeInTheDocument();
    expect(within(surface).getByRole('region', { name: 'Mars 当前任务' })).toBeInTheDocument();
    const earthRow = container.querySelector('[data-row-key="turn-1:participant-earth"]');
    expect(earthRow).not.toBeNull();
    expect(earthRow).toHaveAttribute('data-coordinator', 'true');
    expect(earthRow).not.toHaveTextContent('正在核对 dispatch 回执');
    await user.click(within(earthRow as HTMLElement).getByText('查看工作过程'));
    expect(earthRow).toHaveTextContent('正在核对 dispatch 回执');

    const detailButton = screen.getByRole('button', { name: '展开 Mars 详情' });
    const detailId = detailButton.getAttribute('aria-controls');
    expect(detailId).toBeTruthy();
    await user.click(detailButton);
    expect(screen.getByRole('region', { name: 'Mars 公开进展与证据' })).toHaveAttribute('id', detailId);
    expect(within(surface).getByText('公开进展')).toBeInTheDocument();
    expect(within(surface).getByText('正在核对 dispatch 回执')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '打开 Earth Session' }));
    expect(onOpenParticipant).toHaveBeenCalledWith('participant-earth');
    onOpenParticipant.mockClear();
    await user.click(within(earthRow as HTMLElement).getByRole('button', { name: '打开 Earth Session' }));
    expect(onOpenParticipant).toHaveBeenCalledWith('participant-earth');

    const updated: RoomProjectionState = {
      ...projection,
      activitiesById: {
        ...projection.activitiesById,
        'activity-earth': {
          ...projection.activitiesById['activity-earth']!,
          status: 'completed',
          summary: 'dispatch 回执已经核对完成',
          updatedAtMs: 5,
        },
      },
      turnsById: {
        ...projection.turnsById,
        'turn-1': {
          ...projection.turnsById['turn-1']!,
          terminalParticipantIds: ['participant-earth'],
          updatedAtMs: 5,
        },
      },
    };
    rerender(
      <PawRoomRoundSheet
        onOpenParticipant={onOpenParticipant}
        projection={updated}
        room={room}
      />,
    );

    const updatedEarthRow = container.querySelector('[data-row-key="turn-1:participant-earth"]');
    expect(updatedEarthRow).toBe(earthRow);
    expect(updatedEarthRow).not.toHaveAttribute('data-flowing-light');
    expect(updatedEarthRow).toHaveTextContent('dispatch 回执已经核对完成');
    expect(updatedEarthRow).toHaveTextContent('进行中');
    expect(updatedEarthRow).not.toHaveTextContent('已回复');
  });

  it('keeps the latest objective static and folds only the process with an associated control', async () => {
    const user = userEvent.setup();
    render(
      <PawRoomRoundSheet
        onOpenParticipant={vi.fn()}
        projection={projectionWithProgress('正在工作')}
        room={roomWith([participant('participant-earth', 'session-earth', 0)])}
      />,
    );

    const processToggle = screen.getByRole('button', { name: '收起协作过程' });
    const processId = processToggle.getAttribute('aria-controls');
    expect(processId).toBeTruthy();
    expect(screen.getByText('完成 Room 任务表').closest('button')).toBeNull();
    await user.click(processToggle);

    expect(screen.getByRole('button', { name: '查看协作过程' })).toHaveAttribute('aria-expanded', 'false');
    expect(document.getElementById(processId!)).toHaveAttribute('hidden');
    expect(screen.queryByRole('table', { name: '完成 Room 任务表 · 行星进展' })).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Room 行星任务表' })).toBeInTheDocument();
  });

  it('keeps multiple user rounds as independent collapsible sheets and updates the same row in place', async () => {
    const user = userEvent.setup();
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-mars', 'session-mars', 1),
    ]);
    const initial = projectionWithTwoRounds();
    const { container, rerender } = render(
      <PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={initial} room={room} />,
    );

    const rounds = () => [...container.querySelectorAll<HTMLElement>('[data-round-id]')];
    const [firstRound, secondRound] = rounds();
    expect(firstRound).toBeTruthy();
    expect(secondRound).toBeTruthy();
    expect(firstRound.querySelector('table')).toBeNull();
    expect(secondRound.querySelector('table')).toBeNull();

    // Opening and folding the historical sheet must not change the latest one.
    await user.click(within(firstRound).getByRole('button', { name: '展开本轮任务' }));
    expect(within(firstRound).getByRole('region', { name: 'Earth 当前任务' })).toBeInTheDocument();
    expect(within(secondRound).getByRole('region', { name: 'Mars 当前任务' })).toBeInTheDocument();
    await user.click(within(firstRound).getByRole('button', { name: '折叠本轮任务' }));
    expect(firstRound.querySelector('table')).toBeNull();
    expect(secondRound.querySelector('table')).toBeNull();

    const marsRow = secondRound.querySelector('[data-row-key="turn-2:participant-mars"]');
    expect(marsRow).not.toBeNull();
    expect(marsRow).toHaveTextContent('第二轮正在执行');

    const updated = projectionWithTwoRounds();
    updated.activitiesById['activity-mars'] = {
      ...updated.activitiesById['activity-mars']!,
      status: 'completed',
      summary: '第二轮已经完成并回传证据',
      updatedAtMs: 8,
    };
    updated.turnsById['turn-2'] = {
      ...updated.turnsById['turn-2']!,
      status: 'completed',
      terminalParticipantIds: ['participant-mars'],
      updatedAtMs: 8,
    };
    rerender(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={updated} room={room} />);

    const updatedMarsRow = container.querySelector('[data-row-key="turn-2:participant-mars"]');
    expect(updatedMarsRow).toBe(marsRow);
    expect(updatedMarsRow).toHaveTextContent('第二轮已经完成并回传证据');
    expect(updatedMarsRow).toHaveTextContent('已完成');
    expect(secondRound.querySelector('table')).toBeNull();
    expect(screen.queryByRole('log')).not.toBeInTheDocument();
  });

  it('moves an accepted result into a standalone result planet instead of a table row', async () => {
    const room = roomWith([participant('participant-earth', 'session-earth', 0)]);
    const running = projectionWithProgress('正在验收功能路径');
    const { rerender } = render(
      <PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={running} room={room} />,
    );

    expect(screen.queryByRole('region', { name: 'Earth 公开进展与证据' })).not.toBeInTheDocument();

    const acceptedRoom = {
      ...room,
      workItems: [workItem(
        'work-accepted',
        'turn-1',
        '验收完成，结果已写入 /work/paw/final-result.md。',
        ['/work/paw/final-result.md'],
        5,
      )],
    };
    const accepted: RoomProjectionState = {
      ...running,
      turnsById: {
        ...running.turnsById,
        'turn-1': {
          ...running.turnsById['turn-1']!,
          status: 'completed',
          terminalParticipantIds: ['participant-earth'],
          updatedAtMs: 5,
        },
      },
    };
    const completeAnswer = `验收完成，结果已写入 /work/paw/final-result.md。\n\n${'详细验收依据。'.repeat(90)}\n\n最终保留的结论。`;
    appendCoordinatorPost(
      accepted,
      'result',
      completeAnswer,
    );
    rerender(
      <PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={accepted} room={acceptedRoom} />,
    );

    const result = await screen.findByRole('region', { name: 'Earth 最终结果' });
    expect(result).toHaveAttribute('data-result-ready', 'true');
    expect(result).toHaveTextContent('验收完成');
    expect(result).toHaveTextContent('最终保留的结论。');
    expect(result).not.toHaveAttribute('aria-live');
    expect(within(result).getByRole('status')).toHaveTextContent('已提交');
    expect(within(result).getByRole('status')).not.toHaveTextContent('详细验收依据');
    expect(within(result).getByRole('link', { name: '打开文件 final-result.md' })).toBeInTheDocument();
    expect(within(result).getByRole('button', { name: '打开文件 final-result.md' })).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '收起 Earth 详情' })).not.toBeInTheDocument();
  });

  it('keeps one unfinished collaborator standalone while presenting a submitted planet result separately', async () => {
    const user = userEvent.setup();
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-mars', 'session-mars', 1),
    ]);
    room.workItems = [workItem('work-earth-result', 'turn-1', 'Earth 已提交最终结果。', [], 5)];
    const projection = projectionWithProgress('Earth 已完成');
    projection.turnsById['turn-1'] = {
      ...projection.turnsById['turn-1']!,
      status: 'completed',
      activityIds: ['activity-earth', 'activity-mars'],
      participantIds: ['participant-earth', 'participant-mars'],
      terminalParticipantIds: ['participant-earth'],
      updatedAtMs: 5,
    };
    projection.activitiesById['activity-mars'] = {
      ...projection.activitiesById['activity-earth']!,
      id: 'activity-mars',
      participantId: 'participant-mars',
      sourceSessionId: 'session-mars',
      status: 'running',
      summary: 'Mars 仍在完成剩余任务',
      payload: {
        ...projection.activitiesById['activity-earth']!.payload,
        dispatchId: 'dispatch-mars',
        task: '完成剩余验收',
      },
    };
    projection.activityOrder = ['activity-earth', 'activity-mars'];
    appendCoordinatorPost(
      projection,
      'result',
      'Earth 已提交最终结果。',
    );

    const { container } = render(
      <PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={projection} room={room} />,
    );

    const result = screen.getByRole('region', { name: 'Earth 最终结果' });
    expect(result.closest('table')).toBeNull();
    expect(container.querySelector('[data-row-key="turn-1:participant-earth"]')).toBe(result);
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '查看协作过程' }));
    expect(screen.getByRole('region', { name: 'Mars 当前任务' })).toBeInTheDocument();
  });

  it('keeps the round and planet DOM nodes across a retry-only snapshot, without merging a new user round', () => {
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-mars', 'session-mars', 1),
    ]);
    const firstClientMessageId = 'client-dom-lineage';
    const retryClientMessageId = 'client-dom-retry';
    const optimistic = appendOptimisticRoomMessage(createRoomProjection('room-a'), {
      clientMessageId: firstClientMessageId,
      text: '原始 DOM 轮次',
      nowMs: 1,
    });
    const accepted = reduceRoomEvent(optimistic, parseRoomEvent({
      schemaVersion: 'rag-ime.agent-room-event.v1',
      eventId: 'room-a:1',
      roomId: 'room-a',
      sequence: 1,
      turnId: 'room-turn-authoritative',
      eventType: 'user_message',
      participantId: null,
      sourceSessionId: '',
      createdAtMs: 2,
      payload: {
        messageId: 'room-user-authoritative',
        clientMessageId: firstClientMessageId,
        rootId: 'room-turn-authoritative',
        text: '原始 DOM 轮次',
      },
      resumeToken: 'room-a:1',
    })).state;
    const retrying = appendOptimisticRoomMessage(accepted, {
      clientMessageId: retryClientMessageId,
      text: '原始 DOM 轮次（重试）',
      retryOfRootId: 'room-turn-authoritative',
      nowMs: 3,
    });
    const retryDraft = retrying.messagesById[`local-room:${retryClientMessageId}`]!;
    const retryOnly = applyRoomSnapshot(retrying, {
      messages: [{
        ...retryDraft,
        id: 'room-user-retry-authoritative',
        turnId: 'room-turn-retry-authoritative',
        rootId: 'room-turn-retry-authoritative',
        projectionKind: 'post',
        status: 'completed',
        completedAtMs: 4,
      }],
      lastSequence: 2,
      resumeToken: 'room-a:2',
    });

    const { container, rerender } = render(
      <PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={retrying} room={room} />,
    );
    const roundBefore = container.querySelector('[data-round-id="local-room-turn:client-dom-lineage"]');
    const rowBefore = container.querySelector('[data-row-key="local-room-turn:client-dom-lineage:participant-earth"]');
    expect(roundBefore).not.toBeNull();
    expect(rowBefore).not.toBeNull();

    rerender(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={retryOnly} room={room} />);
    const roundAfter = container.querySelector('[data-round-id="local-room-turn:client-dom-lineage"]');
    const rowAfter = container.querySelector('[data-row-key="local-room-turn:client-dom-lineage:participant-earth"]');
    expect(roundAfter).toBe(roundBefore);
    expect(rowAfter).toBe(rowBefore);

    const withNewRound = reduceRoomEvent(retryOnly, parseRoomEvent({
      schemaVersion: 'rag-ime.agent-room-event.v1',
      eventId: 'room-a:3',
      roomId: 'room-a',
      sequence: 3,
      turnId: 'room-turn-new',
      eventType: 'user_message',
      participantId: null,
      sourceSessionId: '',
      createdAtMs: 5,
      payload: {
        messageId: 'room-user-new',
        clientMessageId: 'client-dom-new',
        rootId: 'room-turn-new',
        text: '完全新的 DOM 轮次',
      },
      resumeToken: 'room-a:3',
    })).state;
    rerender(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={withNewRound} room={room} />);

    expect(container.querySelectorAll('[data-round-id]')).toHaveLength(2);
    expect(container.querySelector('[data-round-id="local-room-turn:client-dom-lineage"]')).toBe(roundBefore);
    expect(container.querySelector('[data-round-id="room-turn-new"]')).not.toBeNull();
    expect(container.querySelector('[data-row-key="room-turn-new:participant-earth"]')).not.toBe(rowBefore);
  });

  it('scrolls only when a new logical user round appears, not for in-place progress updates', async () => {
    const scrollTo = vi.fn();
    const previous = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollTo');
    Object.defineProperty(HTMLElement.prototype, 'scrollTo', {
      configurable: true,
      value: scrollTo,
      writable: true,
    });
    try {
      const room = roomWith([
        participant('participant-earth', 'session-earth', 0),
        participant('participant-mars', 'session-mars', 1),
      ]);
      const first = projectionWithProgress('第一轮正在执行');
      const { rerender } = render(
        <PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={first} room={room} />,
      );
      await waitFor(() => expect(scrollTo).toHaveBeenCalledTimes(1));

      const sameRound = projectionWithProgress('第一轮原地更新');
      rerender(<PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={sameRound} room={room} />);
      await new Promise((resolve) => requestAnimationFrame(resolve));
      expect(scrollTo).toHaveBeenCalledTimes(1);

      rerender(
        <PawRoomRoundSheet
          onOpenParticipant={vi.fn()}
          projection={projectionWithTwoRounds()}
          room={room}
        />,
      );
      await waitFor(() => expect(scrollTo).toHaveBeenCalledTimes(2));
      expect(scrollTo).toHaveBeenLastCalledWith(expect.objectContaining({ behavior: 'smooth' }));
    } finally {
      if (previous) Object.defineProperty(HTMLElement.prototype, 'scrollTo', previous);
      else delete (HTMLElement.prototype as unknown as { scrollTo?: typeof scrollTo }).scrollTo;
    }
  });

  it('shows the authoritative blocker reason and suggested next step in the same row', async () => {
    const user = userEvent.setup();
    const onOpenParticipant = vi.fn();
    const onResumeBlocked = vi.fn();
    const room = roomWith([participant('participant-earth', 'session-earth', 0)]);
    room.workItems = [{
      id: 'work-blocked', roomId: 'room-a', topicId: '', rootTurnId: 'turn-1', rootWorkId: 'work-blocked',
      parentWorkId: '', objective: '检查 Trace API', expectedOutput: '可验证回执', acceptanceCriteria: ['路由可读'],
      accountableParticipantId: 'participant-earth', currentOwnerParticipantId: 'participant-earth',
      offeredToParticipantId: '', createdByParticipantId: 'participant-earth', clientMessageId: 'client-1',
      state: 'blocked', depth: 1, revision: 0, resultSummary: '', artifactRefs: [], evidenceRefs: [],
      blocker: { reason: '缺少 Runtime 路由', nextStep: '恢复 Gateway 后重试' }, acceptedTurnId: '',
      createdAtMs: 2, updatedAtMs: 4, completedAtMs: null,
    }];
    const { rerender } = render(
      <PawRoomRoundSheet
        onOpenParticipant={onOpenParticipant}
        onResumeBlocked={onResumeBlocked}
        projection={projectionWithProgress('等待 Runtime')}
        room={room}
      />,
    );

    expect(screen.getByText('已阻塞')).toBeInTheDocument();
    expect(screen.getByText('缺少 Runtime 路由')).toBeInTheDocument();
    const resumeButton = screen.getByRole('button', { name: '恢复 Earth 并重新分派' });
    await user.click(resumeButton);
    expect(onResumeBlocked).toHaveBeenCalledWith(expect.objectContaining({
      blockedWorkItemId: 'work-blocked',
      state: 'blocked',
    }));
    expect(onOpenParticipant).not.toHaveBeenCalled();
    rerender(
      <PawRoomRoundSheet
        onOpenParticipant={onOpenParticipant}
        onResumeBlocked={onResumeBlocked}
        projection={projectionWithProgress('等待 Runtime')}
        resumeErrorByRow={{ 'turn-1:participant-earth': 'Runtime 拒绝了恢复请求' }}
        room={room}
      />,
    );
    expect(screen.getByRole('button', { name: '重试 Earth 并重新分派' })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Runtime 拒绝了恢复请求');
    await user.click(screen.getByRole('button', { name: '展开 Earth 详情' }));
    expect(screen.getByRole('status')).toHaveTextContent('建议下一步：恢复 Gateway 后重试');
  });

  it('opens absolute result paths and registered file artifacts through the Files surface', async () => {
    const user = userEvent.setup();
    const openRoute = vi.fn();
    const room = roomWith([participant('participant-earth', 'session-earth', 0)]);
    room.artifacts = [{
      id: 'artifact:report',
      roomId: 'room-a',
      topicId: '',
      displayName: 'report.md',
      path: '/work/paw/report.md',
      mediaType: 'text/markdown',
      status: 'active',
      createdAtMs: 1,
      updatedAtMs: 4,
    }];
    room.workItems = [workItem('work-report', 'turn-1', '已写入 /work/paw/summary.md。', ['artifact:report'], 4)];
    const projection = projectionWithProgress('已生成 /work/paw/summary.md');
    projection.turnsById['turn-1'] = {
      ...projection.turnsById['turn-1']!,
      status: 'completed',
      messageIds: ['user-1', 'assistant-1'],
    };
    projection.messagesById['assistant-1'] = {
      id: 'assistant-1',
      roomId: 'room-a',
      turnId: 'turn-1',
      participantId: 'participant-earth',
      sourceSessionId: 'session-earth',
      role: 'assistant',
      status: 'completed',
      text: '已写入 /work/paw/summary.md。',
      postKind: 'result',
      createdAtMs: 4,
    };
    projection.messageOrder = ['user-1', 'assistant-1'];

    render(
      <TooltipProvider>
        <PawOsDesktopProvider openRoute={openRoute} openWindow={() => undefined}>
          <PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={projection} room={room} />
        </PawOsDesktopProvider>
      </TooltipProvider>,
    );

    await user.click(screen.getByRole('button', { name: '打开文件 report.md' }));
    expect(openRoute).toHaveBeenCalledWith('/files?session=session-earth&path=%2Fwork%2Fpaw%2Freport.md');

    const result = screen.getByRole('region', { name: 'Earth 最终结果' });
    await user.click(within(result).getByRole('link', { name: '打开文件 summary.md' }));
    expect(openRoute).toHaveBeenCalledWith('/files?session=session-earth&path=%2Fwork%2Fpaw%2Fsummary.md');
  });

  it('opens relative workspace files named by a Room result', async () => {
    const user = userEvent.setup();
    const openRoute = vi.fn();
    const room = roomWith([participant('participant-earth', 'session-earth', 0)]);
    room.artifacts = [{
      id: 'artifact:report',
      roomId: 'room-a',
      topicId: '',
      displayName: 'final-result.md',
      path: 'docs/final-result.md',
      mediaType: 'text/markdown',
      status: 'active',
      createdAtMs: 1,
      updatedAtMs: 4,
    }];
    room.workItems = [workItem(
      'work-report',
      'turn-1',
      '验收通过，详见 docs/final-result.md。',
      ['artifact:report'],
      4,
    )];
    const projection = projectionWithProgress('已生成 docs/final-result.md');
    projection.turnsById['turn-1'] = {
      ...projection.turnsById['turn-1']!,
      status: 'completed',
      terminalParticipantIds: ['participant-earth'],
    };
    appendCoordinatorPost(
      projection,
      'result',
      '验收通过，详见 docs/final-result.md。',
    );

    render(
      <TooltipProvider>
        <PawOsDesktopProvider openRoute={openRoute} openWindow={() => undefined}>
          <PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={projection} room={room} />
        </PawOsDesktopProvider>
      </TooltipProvider>,
    );

    const result = screen.getByRole('region', { name: 'Earth 最终结果' });
    expect(within(result).getByRole('button', { name: '打开文件 final-result.md' })).toBeInTheDocument();
    const resultLink = within(result).getByRole('link', { name: '打开文件 final-result.md' });
    await user.click(resultLink);
    expect(openRoute).toHaveBeenCalledWith('/files?session=session-earth&path=docs%2Ffinal-result.md');
  });

  it('lets the shared Markdown AST own result file links without rewriting code or punctuation', async () => {
    const user = userEvent.setup();
    const openRoute = vi.fn();
    const room = roomWith([participant('participant-earth', 'session-earth', 0)]);
    room.workItems = [workItem(
      'work-report',
      'turn-1',
      [
        '查看 [报告](docs/report.md)，并保留 `docs/inline.md`。',
        '',
        '```text',
        'docs/fenced.md',
        '```',
        '',
        '补充见 docs/appendix.md。',
      ].join('\n'),
      [],
      4,
    )];
    const projection = projectionWithProgress('报告已完成');
    projection.turnsById['turn-1'] = {
      ...projection.turnsById['turn-1']!,
      status: 'completed',
      terminalParticipantIds: ['participant-earth'],
    };
    appendCoordinatorPost(
      projection,
      'result',
      [
        '查看 [报告](docs/report.md)，并保留 `docs/inline.md`。',
        '',
        '```text',
        'docs/fenced.md',
        '```',
        '',
        '补充见 docs/appendix.md。',
      ].join('\n'),
    );

    render(
      <TooltipProvider>
        <PawOsDesktopProvider openRoute={openRoute} openWindow={() => undefined}>
          <PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={projection} room={room} />
        </PawOsDesktopProvider>
      </TooltipProvider>,
    );

    const detail = screen.getByRole('region', { name: 'Earth 最终结果' });
    expect(within(detail).getByRole('link', { name: '打开文件 report.md' })).toHaveTextContent('报告');
    expect(within(detail).getByText('docs/inline.md').tagName).toBe('CODE');
    expect(within(detail).getByText('docs/fenced.md')).toBeInTheDocument();
    expect(within(detail).queryByRole('link', { name: '打开文件 inline.md' })).not.toBeInTheDocument();
    expect(within(detail).queryByRole('link', { name: '打开文件 fenced.md' })).not.toBeInTheDocument();
    expect(detail).toHaveTextContent('docs/appendix.md。');

    await user.click(within(detail).getByRole('link', { name: '打开文件 report.md' }));
    expect(openRoute).toHaveBeenCalledWith('/files?session=session-earth&path=docs%2Freport.md');
    await user.click(within(detail).getByRole('link', { name: '打开文件 appendix.md' }));
    expect(openRoute).toHaveBeenCalledWith('/files?session=session-earth&path=docs%2Fappendix.md');
  });

  it('renders current task and progress as safe Markdown instead of leaking formatting tokens', () => {
    const projection = projectionWithProgress('**已完成** `Trace`\n\n- 已回执\n- 可复核');
    projection.activitiesById['activity-earth'] = {
      ...projection.activitiesById['activity-earth']!,
      payload: {
        ...projection.activitiesById['activity-earth']!.payload,
        task: '**核对** `Room` 事件',
      },
    };
    const room = roomWith([participant('participant-earth', 'session-earth', 0)]);
    render(
      <PawRoomRoundSheet onOpenParticipant={vi.fn()} projection={projection} room={room} />,
    );

    const task = screen.getByRole('region', { name: 'Earth 当前任务' });
    expect(task.querySelector('.paw-room-round__standalone-task-body section:first-child .agent-markdown strong')).toHaveTextContent('核对');
    expect(task.querySelector('.paw-room-round__standalone-task-body section:first-child .agent-markdown code')).toHaveTextContent('Room');
    expect(task.querySelectorAll('.paw-room-round__standalone-task-body li')).toHaveLength(2);
    expect(task).not.toHaveTextContent('**已完成**');
    expect(task).not.toHaveTextContent('`Trace`');
  });

  it('does not turn a completed planet red because its turn history contains a recoverable tool failure', async () => {
    const user = userEvent.setup();
    const projection = projectionWithProgress('工具失败后已恢复并完成');
    projection.turnsById['turn-1'] = {
      ...projection.turnsById['turn-1']!,
      status: 'completed',
      terminalParticipantIds: [],
    };
    projection.activitiesById['activity-earth'] = {
      ...projection.activitiesById['activity-earth']!,
      status: 'failed',
    };

    render(
      <PawRoomRoundSheet
        onOpenParticipant={vi.fn()}
        projection={projection}
        room={roomWith([participant('participant-earth', 'session-earth', 0)])}
      />,
    );

    await user.click(screen.getByRole('button', { name: '查看协作过程' }));
    const task = screen.getByRole('region', { name: 'Earth 当前任务' });
    expect(task).toHaveTextContent('已完成');
    expect(task).not.toHaveTextContent('需要关注');
    expect(within(task).getByText('工具失败后已恢复并完成')).toBeInTheDocument();
  });
});

function projectionWithProgress(summary: string): RoomProjectionState {
  return {
    ...createRoomProjection('room-a'),
    turnOrder: ['turn-1'],
    turnsById: {
      'turn-1': {
        id: 'turn-1',
        rootId: 'turn-1',
        status: 'running',
        messageIds: ['user-1'],
        activityIds: ['activity-earth'],
        participantIds: ['participant-earth'],
        createdAtMs: 1,
        updatedAtMs: 3,
      },
    },
    messagesById: {
      'user-1': {
        id: 'user-1',
        roomId: 'room-a',
        turnId: 'turn-1',
        participantId: null,
        sourceSessionId: '',
        role: 'user',
        status: 'completed',
        text: '完成 Room 任务表',
        createdAtMs: 1,
      },
    },
    messageOrder: ['user-1'],
    activitiesById: {
      'activity-earth': {
        id: 'activity-earth',
        turnId: 'turn-1',
        participantId: 'participant-earth',
        sourceSessionId: 'session-earth',
        kind: 'participant_activity',
        status: 'running',
        summary,
        payload: {
          rootId: 'turn-1',
          dispatchId: 'dispatch-earth',
          sourceEventType: 'current_progress',
          task: '检查 Room 事件投影',
        },
        createdAtMs: 2,
        updatedAtMs: 3,
      },
    },
    activityOrder: ['activity-earth'],
  };
}

function projectionWithTwoRounds(): RoomProjectionState {
  const first = projectionWithProgress('第一轮已经完成');
  return {
    ...first,
    turnOrder: ['turn-1', 'turn-2'],
    turnsById: {
      ...first.turnsById,
      'turn-1': {
        ...first.turnsById['turn-1']!,
        status: 'completed',
        terminalParticipantIds: ['participant-earth'],
      },
      'turn-2': {
        id: 'turn-2',
        rootId: 'turn-2',
        status: 'running',
        messageIds: ['user-2'],
        activityIds: ['activity-mars'],
        participantIds: ['participant-mars'],
        createdAtMs: 4,
        updatedAtMs: 6,
      },
    },
    messagesById: {
      ...first.messagesById,
      'user-2': {
        id: 'user-2',
        roomId: 'room-a',
        turnId: 'turn-2',
        participantId: null,
        sourceSessionId: '',
        role: 'user',
        status: 'completed',
        text: '第二轮任务',
        createdAtMs: 4,
      },
    },
    messageOrder: ['user-1', 'user-2'],
    activitiesById: {
      ...first.activitiesById,
      'activity-mars': {
        id: 'activity-mars',
        turnId: 'turn-2',
        participantId: 'participant-mars',
        sourceSessionId: 'session-mars',
        kind: 'participant_activity',
        status: 'running',
        summary: '第二轮正在执行',
        payload: {
          rootId: 'turn-2',
          dispatchId: 'dispatch-mars',
          sourceEventType: 'current_progress',
          task: '核对第二轮证据',
        },
        createdAtMs: 5,
        updatedAtMs: 6,
      },
    },
    activityOrder: ['activity-earth', 'activity-mars'],
  };
}

function participant(id: string, sessionId: string, ordinal: number): RoomParticipant {
  return {
    id,
    sessionId,
    roleId: 'implementer',
    roleVersion: '1',
    displayName: `伙伴 ${id}`,
    collaborationRole: ordinal === 0 ? 'coordinator' : 'implementer',
    status: 'active',
    ordinal,
  };
}

function workItem(
  id: string,
  rootTurnId: string,
  resultSummary: string,
  evidenceRefs: string[],
  updatedAtMs: number,
): RoomWorkItem {
  return {
    id,
    roomId: 'room-a',
    topicId: '',
    rootTurnId,
    rootWorkId: id,
    parentWorkId: '',
    objective: `${id} task`,
    expectedOutput: '',
    acceptanceCriteria: [],
    accountableParticipantId: 'participant-earth',
    currentOwnerParticipantId: 'participant-earth',
    offeredToParticipantId: '',
    createdByParticipantId: 'participant-earth',
    clientMessageId: id,
    state: 'done',
    depth: 1,
    revision: 0,
    resultSummary,
    artifactRefs: [],
    evidenceRefs,
    blocker: {},
    acceptedTurnId: rootTurnId,
    createdAtMs: updatedAtMs - 1,
    updatedAtMs,
    completedAtMs: updatedAtMs,
  };
}

function activeWorkItem(id: string, ownerParticipantId: string): RoomWorkItem {
  return {
    ...workItem(id, 'turn-1', '', [], 4),
    accountableParticipantId: ownerParticipantId,
    currentOwnerParticipantId: ownerParticipantId,
    state: 'active',
    completedAtMs: null,
  };
}

function workItemForOwner(id: string, ownerParticipantId: string, resultSummary: string): RoomWorkItem {
  return {
    ...activeWorkItem(id, ownerParticipantId),
    state: 'done',
    resultSummary,
    completedAtMs: 5,
    updatedAtMs: 5,
  };
}

function activityForParticipant(
  base: RoomActivityProjection,
  id: string,
  participantId: string,
  sourceSessionId: string,
  summary: string,
) {
  return {
    ...base,
    id,
    participantId,
    sourceSessionId,
    summary,
    payload: {
      ...base.payload,
      dispatchId: `dispatch-${participantId}`,
      targetParticipantId: participantId,
      task: `${summary} · 任务`,
    },
  };
}

function appendCoordinatorPost(
  projection: RoomProjectionState,
  postKind: 'progress' | 'wait' | 'result',
  text: string,
  createdAtMs = 5,
): void {
  const messageId = `coordinator-post-${postKind}`;
  const turn = projection.turnsById['turn-1'];
  if (!turn) throw new Error('test fixture turn is missing');
  projection.turnsById['turn-1'] = {
    ...turn,
    messageIds: [...turn.messageIds, messageId],
  };
  projection.messagesById[messageId] = {
    id: messageId,
    roomId: 'room-a',
    turnId: 'turn-1',
    participantId: 'participant-earth',
    sourceSessionId: 'session-earth',
    role: 'assistant',
    status: 'completed',
    text,
    projectionKind: 'post',
    postKind,
    rootId: 'turn-1',
    dispatchId: 'dispatch-earth',
    createdAtMs,
    completedAtMs: createdAtMs,
  };
  projection.messageOrder = [...projection.messageOrder, messageId];
}

function roomWith(participants: RoomParticipant[]): RoomSummary {
  return {
    id: 'room-a',
    title: 'Room A',
    status: 'active',
    routingPolicy: 'natural',
    moderatorParticipantId: participants[0]?.id ?? '',
    updatedAtMs: 1,
    participants,
  };
}
