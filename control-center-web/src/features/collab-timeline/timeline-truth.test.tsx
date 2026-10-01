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
  it('keeps superseded WorkItems out of current task counts without removing dispatch history', () => {
    const demo = collabDemoRoom();
    const rootId = Object.keys(demo.projection.turnsById)[0]!;
    const graph = parseJevSnapshot({
      ok: true, mode: 'jev', graphId: 'revised-graph', roomId: demo.room.id, rootId,
      snapshotVersion: 'v2', phase: 'execute', activeTaskIds: ['replacement'], tasks: [
        { id: 'w-mars', state: 'cancelled', owner_id: 'p-mars', objective: '旧任务' },
        { id: 'replacement', state: 'active', owner_id: 'p-mars', objective: '修订后的任务' },
      ], effects: [], edges: [], ready: [], running: [], review: [], blocked: [], events: [], modelCards: [],
    }, 'revised-graph');
    const model = buildRoomCollabTimeline({ ...demo, graph });
    expect(model.tasks?.some(task => task.id === 'w-mars')).toBe(false);
    expect(model.tasks?.some(task => task.id === 'replacement')).toBe(true);
    expect(model.dispatches?.some(dispatch => dispatch.id === 'd-mars')).toBe(true);
  });

  it('distinguishes blocked and executing tasks from the active WorkItem state', async () => {
    const demo = collabDemoRoom({ cut: 0 });
    const rootId = Object.keys(demo.projection.turnsById)[0]!;
    const graph = parseJevSnapshot({
      ok: true, mode: 'jev', graphId: 'dependency-graph', roomId: demo.room.id, rootId,
      snapshotVersion: 'v1', phase: 'execute', tasks: [
        { id: 'foundation', state: 'review', revision: 0, owner_id: 'p-earth', objective: '基础契约' },
        { id: 'game', state: 'active', revision: 0, owner_id: 'p-mars', objective: '游戏规则' },
        { id: 'scene', state: 'active', revision: 0, owner_id: 'p-venus', objective: '画面实现' },
      ], effects: [
        { effectId: 'verify', operation: 'dispatch', state: 'accepted', executionStatus: 'running', request: { taskId: 'foundation', taskRevision: 0, purpose: 'verify' }, receipt: {} },
        { effectId: 'execute', operation: 'dispatch', state: 'accepted', executionStatus: 'running', request: { taskId: 'scene', taskRevision: 0, purpose: 'execute' }, receipt: {} },
      ], edges: [{ prerequisite: 'foundation', dependent: 'game', kind: 'requires' }],
      ready: [], running: ['scene'], review: ['foundation'], blocked: [{ taskId: 'game', reasons: ['dependency:foundation'] }], events: [], modelCards: [],
    }, 'dependency-graph');
    const model = buildRoomCollabTimeline({ ...demo, graph });
    expect(model.tasks?.find(task => task.id === 'game')).toMatchObject({ state: 'blocked', stateLabel: '等待依赖', waitingOn: ['基础契约'] });
    expect(model.tasks?.find(task => task.id === 'foundation')?.stateLabel).toBe('复核中');
    expect(model.tasks?.find(task => task.id === 'scene')?.stateLabel).toBe('执行中');
    render(<CollabTimelineStage timeline={model} active={false} />);
    await userEvent.click(screen.getByRole('button', { name: '任务与分派' }));
    const game = screen.getByText('游戏规则').closest('details')!;
    expect(game).toHaveTextContent('等待依赖');
    await userEvent.click(within(game).getByText('游戏规则'));
    expect(within(game).getByText('等待前置：基础契约')).toBeVisible();
  });

  it('does not equate a child output contract with task acceptance or a parent final reply', () => {
    const child = run();
    child.contract.status = 'valid';
    const model = buildSessionCollabTimeline({ sessionId: 's', title: '主对话', runs: [child] });
    expect(model.counts.accepted).toBe(0);
    expect(model.counts.returned).toBe(1);
    expect(model.final).toBe(false);
    expect(model.settled).toBe(true);
    expect(model.marks.some(mark => mark.kind === 'accept')).toBe(false);
    expect(model.phases.find(phase => phase.key === 'reply')).toBeUndefined();
    expect(model.phases.find(phase => phase.key === 'accept')).toBeUndefined();
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

  it('does not treat a reviewer role as an observed review dispatch', () => {
    const demo = collabDemoRoom();
    const reviewRoute = Object.values(demo.projection.activitiesById).find(activity => (
      activity.kind === 'route_decision' && activity.payload.targetParticipantId === 'p-jupiter'
    ));
    expect(reviewRoute).toBeDefined();
    const projection = {
      ...demo.projection,
      activitiesById: {
        ...demo.projection.activitiesById,
        [reviewRoute!.id]: { ...reviewRoute!, payload: { ...reviewRoute!.payload, purpose: undefined } },
      },
    };
    const model = buildRoomCollabTimeline({ room: demo.room, projection, nowMs: demo.nowMs });
    expect(model.lanes.find(lane => lane.id === 'p-jupiter')?.role).toBe('复核伙伴');
    expect(model.segments.filter(segment => segment.laneId === 'p-jupiter').every(segment => segment.kind === 'execute')).toBe(true);
    expect(model.phases.find(phase => phase.key === 'review')).toBeUndefined();
  });

  it('omits unobserved stages for a direct answer instead of showing five placeholders', () => {
    const direct = collabDemoRoom({ cut: 0 });
    const model = buildRoomCollabTimeline({ room: direct.room, projection: direct.projection, nowMs: direct.nowMs });
    expect(model.phases).toEqual([]);
  });

  it('keeps a failed graph result-aware: real task denominator, failed core stages, unfinished reply', () => {
    const demo = collabDemoRoom();
    const rootId = Object.keys(demo.projection.turnsById)[0]!;
    const turn = demo.projection.turnsById[rootId]!;
    turn.status = 'failed';
    turn.updatedAtMs = demo.nowMs;
    turn.rootTerminalAtMs = demo.nowMs;
    const verifyTerminal = Object.values(demo.projection.activitiesById).find(activity => (
      activity.payload.activityKind === 'child' && activity.payload.childDispatchId === 'd-jupiter'
    ));
    expect(verifyTerminal).toBeDefined();
    demo.projection.activitiesById[verifyTerminal!.id] = {
      ...verifyTerminal!, status: 'failed', payload: { ...verifyTerminal!.payload, phase: 'failed', status: 'failed' },
    };
    const graph = parseJevSnapshot({
      ok: true, mode: 'jev', graphId: 'failed-graph', roomId: demo.room.id, rootId,
      snapshotVersion: 'failed-v1', phase: 'final', tasks: [
        { id: 'goal', state: 'queued', parent_id: '', owner_id: 'p-earth', objective: '合成总目标' },
        { id: 'task-a', state: 'failed', parent_id: 'goal', owner_id: 'p-mars', objective: '执行任务 A' },
        { id: 'task-b', state: 'queued', parent_id: 'goal', owner_id: 'p-venus', objective: '执行任务 B' },
      ], final: { content: '部分答复', status: 'failed', evidence: [] }, effects: [], edges: [], ready: [], running: [], review: [], blocked: [], events: [], modelCards: [],
    }, 'failed-graph');
    const model = buildRoomCollabTimeline({ room: demo.room, projection: demo.projection, graph, nowMs: demo.nowMs });
    expect(model.counts).toMatchObject({ accepted: 0, total: 2 });
    expect(model.phases.find(phase => phase.key === 'accept')).toBeUndefined();
    expect(model.phases.find(phase => phase.key === 'execute')?.state).toBe('failed');
    expect(model.phases.find(phase => phase.key === 'review')?.state).toBe('failed');
    expect(model.phases.find(phase => phase.key === 'reply')?.state).toBe('failed');
    expect(model.marks.find(mark => mark.kind === 'final_unfinished')?.label).toContain('未完成');
    render(<CollabTimelineStage timeline={model} active={false} />);
    const stage = screen.getByRole('region', { name: '多 Agent 协作时间线' });
    expect(stage).toHaveAttribute('data-motion', 'off');
    expect(stage.querySelector('[data-pop]')).toBeNull();
    expect(stage.querySelector('.ctl-stamp[data-state="unfinished"]')).not.toBeNull();
  });

  it('does not mark partial acceptance complete while a failed task awaits the final report', () => {
    const demo = collabDemoRoom();
    const rootId = Object.keys(demo.projection.turnsById)[0]!;
    demo.projection.turnsById[rootId]!.status = 'running';
    const graph = parseJevSnapshot({
      ok: true, mode: 'jev', graphId: 'partial-graph', roomId: demo.room.id, rootId,
      snapshotVersion: 'partial-v1', phase: 'execute', tasks: [
        { id: 'accepted', state: 'done', owner_id: 'p-mars', objective: '独立模块已验收' },
        { id: 'failed', state: 'failed', owner_id: 'p-venus', objective: '核心核验失败' },
      ], effects: [{ effectId: 'summary', operation: 'dispatch', request: { purpose: 'synthesize' } }],
      edges: [], ready: [], running: [], review: [], blocked: [], events: [], modelCards: [],
    }, 'partial-graph');
    const model = buildRoomCollabTimeline({ ...demo, graph });
    expect(model.counts).toMatchObject({ accepted: 1, total: 2 });
    expect(model.phases.find(phase => phase.key === 'accept')?.state).toBe('failed');
    expect(model.phases.find(phase => phase.key === 'execute')?.state).toBe('failed');
    expect(model.phases.find(phase => phase.key === 'reply')?.state).toBe('current');
    graph.tasks[1]!.state = 'active';
    const ongoing = buildRoomCollabTimeline({ ...demo, graph });
    expect(ongoing.phases.find(phase => phase.key === 'accept')?.state).toBe('current');
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
