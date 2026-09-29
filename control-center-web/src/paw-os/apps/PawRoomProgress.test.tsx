import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PawRoomWorkStatus } from './PawRoomWorkStatus';
import { PawRoomProgress } from './PawRoomProgress';
import { PawRoomRoutingEvidence } from './PawRoomRoutingEvidence';
import { buildRoomVisualProgress } from './room-visual-progress';
import { projectRoomRoutes } from './room-route-visibility';
import type { RoomWorkStatus } from './room-work-status';
import { visualActivity, visualFocus, visualRoster, visualTurn, visualWork } from '@/test/fixtures/room-visual';

afterEach(cleanup);
const status: RoomWorkStatus = { state: 'running', headline: '正在核验', detail: '正在读取测试结果', animate: true, live: true,
  updatedAtMs: 100, completed: 0, total: 2, review: 0, executingParticipantIds: ['mars'], action: 'inspect' };
const props = () => ({ focus: visualFocus([visualWork('a'), visualWork('b', { state: 'blocked', blocker: { reason: '等待材料' } })]), status,
  onOpenParticipant: vi.fn(), onRetrySync: vi.fn(), onAnswer: vi.fn() });

describe('Task count progress and role-first workflow', () => {
  it('exposes a count range, excludes aggregate tasks, and never claims elapsed-time percent', () => {
    const progress = buildRoomVisualProgress(visualFocus([visualWork('parent', { state: 'completed' }), visualWork('a', { parentId: 'parent', state: 'completed' }), visualWork('b', { parentId: 'parent' })]));
    render(<PawRoomProgress progress={progress} live onSelect={vi.fn()} />);
    const bar = screen.getByRole('progressbar', { name: '当前执行项完成数量' });
    expect(bar).toHaveAttribute('aria-valuemax', '2'); expect(bar).toHaveAttribute('aria-valuenow', '1');
    expect(bar.getAttribute('aria-valuetext')).toContain('不是耗时进度');
    expect(screen.queryByText('50%')).not.toBeInTheDocument();
  });
  it('runtime-only and incomplete records have no numeric progress bar', () => {
    const view = render(<PawRoomProgress progress={buildRoomVisualProgress(visualFocus([]))} live onSelect={vi.fn()} />);
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    view.rerender(<PawRoomProgress progress={buildRoomVisualProgress(visualFocus([visualWork('x', { parentId: 'missing' })]))} live onSelect={vi.fn()} />);
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument(); expect(screen.getByText(/上层任务尚未同步/)).toBeInTheDocument();
  });
  it('keyboard-operable state legend opens and filters the real assignment view', async () => {
    const user = userEvent.setup(); render(<PawRoomWorkStatus {...props()} />);
    const filter = screen.getByRole('button', { name: '查看受阻执行项，1 项' }); filter.focus(); await user.keyboard('{Enter}');
    expect(filter).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: '收起任务' })).toHaveAttribute('aria-expanded', 'true');
    const map = screen.getByLabelText('任务分派图'); expect(within(map).getByRole('button', { name: /核验 b/ })).toBeInTheDocument();
    expect(within(map).queryByRole('button', { name: /核验 a/ })).not.toBeInTheDocument();
    expect(screen.getByLabelText('当前协作分工')).not.toBeVisible();
  });
  it('selecting a progress segment from the relations view returns to tasks', async () => {
    const user = userEvent.setup(); render(<PawRoomWorkStatus {...props()} />);
    await user.click(screen.getByRole('button', { name: '展开任务' }));
    await user.click(screen.getByRole('button', { name: /协作往来/ }));
    await user.click(screen.getByRole('button', { name: '查看受阻执行项，1 项' }));
    expect(screen.getByLabelText('任务分派图')).toBeVisible();
  });
  it('keeps a manually selected task as its state changes out of the filter', async () => {
    const user = userEvent.setup(); const p = props(); const view = render(<PawRoomWorkStatus {...p} />);
    await user.click(screen.getByRole('button', { name: '查看受阻执行项，1 项' }));
    await user.click(within(screen.getByLabelText('任务分派图')).getByRole('button', { name: /核验 b/ }));
    view.rerender(<PawRoomWorkStatus {...p} focus={visualFocus([visualWork('a'), visualWork('b', { state: 'review' })])} />);
    expect(screen.getByLabelText('分工详情')).toHaveTextContent('核验 b');
    expect(screen.getByText(/正在保留你选中的任务/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '查看受阻执行项，0 项' })).toHaveAttribute('aria-pressed', 'true');
  });
  it('new Root clears the old state filter without remounting the Room composer', async () => {
    const user = userEvent.setup(); const p = props(); const view = render(<PawRoomWorkStatus {...p} />);
    await user.click(screen.getByRole('button', { name: '查看受阻执行项，1 项' }));
    const next = visualFocus([visualWork('new', { state: 'completed' })]); next.goal.rootId = 'root-2';
    view.rerender(<PawRoomWorkStatus {...p} focus={next} />);
    expect(within(screen.getByLabelText('任务分派图')).getByRole('button', { name: /核验 new/ })).toBeInTheDocument();
    expect(screen.queryByText('正在保留你选中的任务')).not.toBeInTheDocument();
  });
  it('shows role and identity together and navigation does not dispatch', async () => {
    const user = userEvent.setup(); const p = props(); render(<PawRoomWorkStatus {...p} />);
    await user.click(screen.getByRole('button', { name: '展开任务' }));
    const team = screen.getByLabelText('当前协作分工'); expect(team).toHaveTextContent('统筹'); expect(team).toHaveTextContent('实现');
    await user.click(within(team).getByRole('button', { name: '打开 Mars 的实际会话' }));
    expect(p.onOpenParticipant).toHaveBeenCalledExactlyOnceWith('mars'); expect(p.onRetrySync).not.toHaveBeenCalled();
  });
  it('retains the full long task behind an explicit disclosure, including non-ASCII text', async () => {
    const user = userEvent.setup(); const full = 'Fix the source with exact regression coverage. '.repeat(6) + '保留原文。';
    const p = props(); p.focus = visualFocus([visualWork('long', { objective: full })]); render(<PawRoomWorkStatus {...p} />);
    await user.click(screen.getByRole('button', { name: '展开任务' }));
    const detail = screen.getByLabelText('分工详情'); fireEvent.click(within(detail).getByText('查看完整任务'));
    expect(within(detail).getByText(full.trim())).toBeVisible();
  });
  it('offline retains the old count while suppressing live execution animation', () => {
    const p = props(); const view = render(<PawRoomWorkStatus {...p} status={{ ...status, state: 'offline', live: false, animate: false, action: 'sync' }} />);
    expect(screen.getByText(/上次记录/)).toBeInTheDocument(); expect(view.container.querySelector('.paw-room-work-status__spin')).toBeNull();
    expect(screen.getByRole('button', { name: '重新同步' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '展开任务' }));
    expect(screen.getByLabelText('当前协作分工')).toHaveTextContent('离线 · 上次');
    expect(screen.getByLabelText('当前协作分工')).not.toHaveTextContent('恢复中');
  });
});

describe('Read-only Jev receipts', () => {
  const routes = () => projectRoomRoutes([visualActivity('r', { payload: { reason: 'jev', dispatchId: 'd', targetParticipantId: 'mars', jev: { status: 'selected', choice: 'mars', confidence: .88, sourceRevision: 'v' } } })], visualRoster, visualTurn());
  it('selected Choice remains unstarted and confidence is never shown as a progressbar', () => {
    render(<PawRoomRoutingEvidence routes={routes()} live onOpenParticipant={vi.fn()} />);
    fireEvent.click(screen.getByText(/分派依据 ·/));
    expect(screen.getByText('已记录路由 · 尚无开始回执')).toBeVisible();
    expect(screen.getByText(/决策置信度 0.88/)).toBeVisible(); expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  });
  it('discloses raw provenance without sending a model or dispatch request', () => {
    const open = vi.fn(); render(<PawRoomRoutingEvidence routes={routes()} live onOpenParticipant={open} />);
    fireEvent.click(screen.getByText(/分派依据 ·/)); fireEvent.click(screen.getByText('查看路由与执行依据'));
    expect(screen.getByText(/"sourceRevision": "v"/)).toBeVisible(); expect(open).not.toHaveBeenCalled();
  });
  it('missing receipts do not claim Jev is configured, enabled, or failed', () => {
    render(<PawRoomRoutingEvidence routes={[]} live onOpenParticipant={vi.fn()} />);
    fireEvent.click(screen.getByText('分派依据'));
    expect(screen.getByText(/不能据此判断账号配置/)).toBeVisible();
  });
  it('retains historical removed participants but disables their navigation', () => {
    const rows = routes().map((item) => ({ ...item, canOpen: false }));
    render(<PawRoomRoutingEvidence routes={rows} live onOpenParticipant={vi.fn()} />);
    fireEvent.click(screen.getByText(/分派依据 ·/)); expect(screen.getByRole('button', { name: 'mars · 会话' })).toBeDisabled();
  });
});
