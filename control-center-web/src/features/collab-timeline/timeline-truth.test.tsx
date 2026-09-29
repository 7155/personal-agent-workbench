import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { parseJevSnapshot } from '@/features/semantic-workspace/jev-execution';
import { collabDemoRoom, subagentRun } from './fixtures';
import { buildSessionCollabTimeline } from './session-timeline';
import { buildRoomCollabTimeline } from './room-timeline';
import { collabPhasesFromEvidence, collabFocusAt } from './model';
import { CollabTimelineStage } from './CollabTimelineStage';
import { SessionCollabTimeline } from './SessionCollabTimeline';

afterEach(cleanup);

const base = Date.UTC(2026, 8, 29, 9);
const run = () => subagentRun({ id: 'a', parent: 's', task: '检查文本', template: 'worker', createdAtMs: base, completedAtMs: base + 20_000, tools: 3 });

describe('timeline evidence boundaries', () => {
  it('does not equate a child output contract with task acceptance or a parent final reply', () => {
    const child = run();
    child.contract.status = 'valid';
    const model = buildSessionCollabTimeline({ sessionId: 's', title: '主对话', runs: [child] });
    expect(model.counts.accepted).toBe(0);
    expect(model.counts.returned).toBe(1);
    expect(model.final).toBe(false);
    expect(model.settled).toBe(true);
    expect(model.marks.some(mark => mark.kind === 'accept')).toBe(false);
    expect(model.phases.find(phase => phase.key === 'reply')?.state).toBe('pending');
    expect(model.phases.find(phase => phase.key === 'accept')?.state).toBe('pending');
  });

  it('does not invent tool timestamps from a usage count', () => {
    const model = buildSessionCollabTimeline({ sessionId: 's', title: '主对话', runs: [run()] });
    expect(model.counts.tools).toBe(3);
    expect(model.marks.filter(mark => mark.kind === 'tool')).toEqual([]);
  });

  it('keeps queued children waiting and invalid output visibly unsuccessful', () => {
    const queued = { ...run(), state: 'queued' as const, startedAtMs: null, completedAtMs: null };
    const model = buildSessionCollabTimeline({ sessionId: 's', title: '主对话', runs: [queued], nowMs: base + 30_000 });
    expect(model.segments.filter(segment => segment.laneId === 'run:a').every(segment => segment.kind === 'wait')).toBe(true);
    const invalid = run(); invalid.contract.status = 'invalid';
    const result = buildSessionCollabTimeline({ sessionId: 's', title: '主对话', runs: [invalid] });
    expect(result.counts.returned).toBe(0);
    expect(result.counts.failed).toBe(1);
    expect(result.lanes.find(lane => lane.id === 'run:a')?.state).toBe('error');
  });

  it('does not check off stages that were never observed', () => {
    const phases = collabPhasesFromEvidence({ reached: new Set(['execute', 'reply']), final: true });
    expect(phases.filter(phase => phase.state === 'done').map(phase => phase.key)).toEqual(['execute', 'reply']);
  });

  it('does not let an unrelated graph mark the selected Room round complete', () => {
    const demo = collabDemoRoom({ cut: 35 });
    const graph = parseJevSnapshot({ ok: true, mode: 'jev', graphId: 'other', snapshotVersion: 'fixture-1', phase: 'final', rootId: 'another-root', tasks: [], final: { content: '旧答复', status: 'completed' } }, 'other');
    const model = buildRoomCollabTimeline({ room: demo.room, projection: demo.projection, nowMs: demo.nowMs, graph });
    expect(model.final).toBe(false);
    expect(model.live).toBe(true);
  });

  it('does not move the main handoff to a pending submission or side message', () => {
    const demo = collabDemoRoom();
    const model = buildRoomCollabTimeline({ room: demo.room, projection: demo.projection, nowMs: demo.nowMs });
    const previous = collabFocusAt(model, model.endMs);
    model.handoffs.push({ id: 'pending', kind: 'submit', pending: true, fromLaneId: 'p-earth', toLaneId: 'p-mars', atMs: model.endMs, label: '待提交' });
    expect(collabFocusAt(model, model.endMs)).toEqual(previous);
  });
});

describe('timeline interaction continuity', () => {
  it('opens a completed snapshot at the end rather than auto-replaying it', () => {
    const demo = collabDemoRoom();
    const model = buildRoomCollabTimeline({ room: demo.room, projection: demo.projection, nowMs: demo.nowMs });
    render(<CollabTimelineStage timeline={model} />);
    expect(screen.getByRole('slider', { name: '回放进度' })).toHaveValue('1000');
    expect(screen.queryByRole('button', { name: '暂停回放' })).not.toBeInTheDocument();
  });

  it('lets Space activate the focused lane rather than hijacking it for replay', async () => {
    const demo = collabDemoRoom();
    const model = buildRoomCollabTimeline({ room: demo.room, projection: demo.projection, nowMs: demo.nowMs });
    const onOpen = vi.fn();
    render(<CollabTimelineStage timeline={model} onOpenLane={onOpen} />);
    screen.getByRole('button', { name: /^Mars，/ }).focus();
    await userEvent.keyboard(' ');
    expect(onOpen).toHaveBeenCalledOnce();
    expect(screen.getByRole('slider', { name: '回放进度' })).toHaveValue('1000');
  });

  it('uses the shared dialog, keeps keyboard focus inside, and returns it on Escape', async () => {
    render(<SessionCollabTimeline sessionId="s" title="主对话" runs={[run()]} active />);
    const user = userEvent.setup();
    const trigger = screen.getByRole('button', { name: '打开卫星协作时间线' });
    await user.click(trigger);
    const dialog = screen.getByRole('dialog', { name: '卫星协作时间线' });
    expect(within(dialog).getByText(/回放不会重新执行任务/)).toBeVisible();
    const close = within(dialog).getByRole('button', { name: '关闭' });
    close.focus();
    await user.tab();
    expect(dialog.contains(document.activeElement)).toBe(true);
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });
});
