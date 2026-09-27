import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PawRoomAssignmentMap } from './PawRoomAssignmentMap';
import { PawRoomWorkStatus } from './PawRoomWorkStatus';
import { PawRoomFocusParticipantBar } from './PawRoomFocusParticipants';
import type { RoomFocusProjection } from './room-focus-projection';
import type { RoomWorkStatus } from './room-work-status';

afterEach(cleanup);
function fixture(): RoomFocusProjection {
  return {
    goal: { title: '核对恢复', description: '', rootId: 'root-a', state: 'running' },
    partners: [{ participantId: 'mars', sessionId: 's-mars', displayName: '恢复核验', celestialName: 'Mars',
      state: 'running', currentAction: '检查回放', ownedWorkItemIds: ['a', 'b'], unread: false }],
    workItems: [
      { id: 'a', source: 'work-item', objective: '执行订阅检查', ownerParticipantId: 'mars', state: 'running',
        acceptanceCriteria: ['唯一执行回执'], reviewRequired: false, evidence: [], updatedAtMs: 30 },
      { id: 'b', source: 'work-item', objective: '核对历史缺口', ownerParticipantId: 'mars', state: 'blocked',
        blocker: { reason: '缺少连续事件', nextStep: '读取完整快照，不重新派发' }, acceptanceCriteria: ['唯一缺口回执'],
        reviewRequired: false, evidence: [], updatedAtMs: 40 },
    ],
    counts: { active: 1, review: 0, blocked: 1, completed: 0 }, handoffs: [], flow: [], rootEvidence: [],
  };
}
const status: RoomWorkStatus = { state: 'running', headline: '正在核对', detail: 'Mars · 检查回放',
  animate: true, live: true, updatedAtMs: 40, total: 2, completed: 0, review: 0,
  executingParticipantIds: ['mars'], action: 'inspect' };
function dock(focus: RoomFocusProjection) {
  return <PawRoomWorkStatus focus={focus} status={status} onOpenParticipant={vi.fn()} onRetrySync={vi.fn()} onAnswer={vi.fn()} />;
}
function bar(focus: RoomFocusProjection) {
  return <PawRoomFocusParticipantBar focus={focus} satellitesByParticipant={{}} onSelect={vi.fn()} onCloseInspector={vi.fn()} />;
}
const graph = () => screen.getByLabelText('任务分派图');
const detail = () => screen.getByLabelText('分工详情');

describe('Room task reader choices', () => {
  it('filters to actual attention states without altering work item totals', async () => {
    const user = userEvent.setup(); render(<PawRoomAssignmentMap focus={fixture()} onOpenParticipant={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: '需处理 1' }));
    expect(within(graph()).queryByRole('button', { name: /执行订阅检查/ })).not.toBeInTheDocument();
    expect(within(graph()).getByRole('button', { name: /核对历史缺口/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '全部 2' })).toBeInTheDocument();
  });
  it('keeps a selected task detail when the task leaves the active filter', async () => {
    const user = userEvent.setup(), focus = fixture();
    const view = render(<PawRoomAssignmentMap focus={focus} onOpenParticipant={vi.fn()} />);
    await user.click(within(graph()).getByRole('button', { name: /执行订阅检查/ }));
    await user.click(screen.getByRole('button', { name: '执行中 1' }));
    const next = fixture(); next.workItems[0].state = 'completed';
    view.rerender(<PawRoomAssignmentMap focus={next} onOpenParticipant={vi.fn()} />);
    expect(detail()).toHaveTextContent('唯一执行回执');
    expect(screen.getByRole('button', { name: '显示所选任务' })).toBeInTheDocument();
    expect(within(graph()).queryByRole('button', { name: /执行订阅检查/ })).not.toBeInTheDocument();
  });
  it('searches acceptance text and actual participant names literally', async () => {
    const user = userEvent.setup(); render(<PawRoomAssignmentMap focus={fixture()} onOpenParticipant={vi.fn()} />);
    await user.type(screen.getByRole('searchbox', { name: '搜索任务或伙伴' }), 'Mars 唯一缺口回执');
    expect(within(graph()).getByRole('button', { name: /核对历史缺口/ })).toBeInTheDocument();
    expect(within(graph()).queryByRole('button', { name: /执行订阅检查/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '清除搜索' }));
    expect(within(graph()).getByRole('button', { name: /执行订阅检查/ })).toBeInTheDocument();
  });
  it('does not interpret search text as markup and can clear no-match filters', async () => {
    const user = userEvent.setup(); const view = render(<PawRoomAssignmentMap focus={fixture()} onOpenParticipant={vi.fn()} />);
    await user.type(screen.getByRole('searchbox'), '<img src=x>');
    expect(view.container.querySelector('img')).toBeNull();
    await user.click(screen.getByRole('button', { name: '查看全部任务' }));
    expect(screen.getByRole('searchbox')).toHaveValue('');
    expect(within(graph()).getByRole('button', { name: /执行订阅检查/ })).toBeInTheDocument();
  });
  it('discloses a removed selected task rather than silently changing its detail', async () => {
    const user = userEvent.setup(); const view = render(<PawRoomAssignmentMap focus={fixture()} onOpenParticipant={vi.fn()} />);
    await user.click(within(graph()).getByRole('button', { name: /执行订阅检查/ }));
    const next = fixture(); next.workItems = next.workItems.filter((task) => task.id !== 'a');
    view.rerender(<PawRoomAssignmentMap focus={next} onOpenParticipant={vi.fn()} />);
    expect(detail()).toHaveTextContent('所选任务已不在当前投影中');
    expect(detail()).not.toHaveTextContent('唯一缺口回执');
  });
  it('cannot open a missing Session but still shows evidence and next step', () => {
    const focus = fixture(); focus.partners[0].sessionId = '';
    const open = vi.fn(); render(<PawRoomAssignmentMap focus={focus} onOpenParticipant={open} />);
    const button = screen.getByRole('button', { name: '查看实际会话与调用' });
    expect(button).toBeDisabled(); fireEvent.click(button); expect(open).not.toHaveBeenCalled();
    expect(detail()).toHaveTextContent('读取完整快照，不重新派发');
    expect(detail()).toHaveTextContent('Session 尚未同步');
  });
  it('opens the exact participant and does not dispatch from a filter click', async () => {
    const user = userEvent.setup(), open = vi.fn(); render(<PawRoomAssignmentMap focus={fixture()} onOpenParticipant={open} />);
    await user.click(screen.getByRole('button', { name: '需处理 1' })); expect(open).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '查看实际会话与调用' }));
    expect(open).toHaveBeenCalledExactlyOnceWith('mars');
  });
  it('uses terminal task status even when a historical offer remains', () => {
    const focus = fixture(); focus.workItems = [focus.workItems[0]];
    focus.workItems[0].ownerParticipantId = undefined; focus.workItems[0].offeredToParticipantId = 'mars'; focus.workItems[0].state = 'failed';
    render(<PawRoomAssignmentMap focus={focus} onOpenParticipant={vi.fn()} />);
    expect(within(graph()).getByRole('button', { name: /执行订阅检查/ })).not.toHaveTextContent('待接收');
    expect(detail()).toHaveTextContent('曾邀接收');
  });
  it('resets the task reader only when the composer moves to a different Root', async () => {
    const user = userEvent.setup(); const view = render(dock(fixture()));
    await user.click(screen.getByRole('button', { name: '展开任务' }));
    await user.type(screen.getByRole('searchbox'), '唯一缺口回执');
    const next = fixture(); next.goal.rootId = 'root-b'; next.workItems = [next.workItems[0]];
    view.rerender(dock(next));
    expect(screen.getByRole('searchbox')).toHaveValue('');
    expect(detail()).toHaveTextContent('唯一执行回执');
  });
  it('retains selection and search when the header map is closed and opened', async () => {
    const user = userEvent.setup(); render(bar(fixture()));
    await user.click(screen.getByRole('button', { name: '任务关系' }));
    await user.click(within(graph()).getByRole('button', { name: /执行订阅检查/ }));
    await user.type(screen.getByRole('searchbox'), 'Mars');
    await user.click(screen.getByRole('button', { name: '关闭任务关系' }));
    expect(graph()).not.toBeVisible();
    await user.click(screen.getByRole('button', { name: '任务关系' }));
    expect(detail()).toHaveTextContent('唯一执行回执'); expect(screen.getByRole('searchbox')).toHaveValue('Mars');
  });
  it('uses distinct DOM targets for two windows showing the same Root', () => {
    render(<>{bar(fixture())}{bar(fixture())}</>);
    const ids = screen.getAllByRole('button', { name: '任务关系' }).map((button) => button.getAttribute('aria-controls'));
    expect(new Set(ids).size).toBe(2);
  });
  it('header Escape closes only that map and restores its trigger', async () => {
    const user = userEvent.setup(); render(bar(fixture()));
    const trigger = screen.getByRole('button', { name: '任务关系' }); await user.click(trigger);
    fireEvent.keyDown(screen.getByRole('searchbox'), { key: 'Escape' });
    expect(graph()).not.toBeVisible(); expect(trigger).toHaveFocus();
  });
});
