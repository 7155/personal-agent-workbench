import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { collabDemoRoom, subagentRun } from '@/test/fixtures/collab-timeline';
import { CollabTimelinePeek, CollabTimelineStage } from './CollabTimelineStage';
import { collabLaneStateAt, collabTimeScale, COLLAB_GAP_MS } from './model';
import { buildRoomCollabTimeline, ORIGIN_LANE } from './room-timeline';
import { buildSessionCollabTimeline, SESSION_LANE } from './session-timeline';

afterEach(cleanup);

describe('Room collaboration timeline projection', () => {
  const demo = collabDemoRoom();
  const timeline = buildRoomCollabTimeline({ room: demo.room, projection: demo.projection, satellites: demo.satellites, nowMs: demo.nowMs });

  it('draws the coordinator, the delegated planets and the Tool Agent satellite under its owner', () => {
    expect(timeline.lanes.map((lane) => lane.id)).toEqual([ORIGIN_LANE, 'p-earth', 'p-mars', 'p-venus', 'sat:sat-1', 'p-jupiter']);
    expect(timeline.lanes.find((lane) => lane.id === 'p-earth')?.role).toBe('行星协调');
    expect(timeline.lanes.find((lane) => lane.id === 'sat:sat-1')).toMatchObject({ kind: 'satellite', parentId: 'p-venus' });
  });

  it('attributes child dispatches to the planet that delegated them, not to the Room', () => {
    const toMars = timeline.handoffs.find((handoff) => handoff.id === 'd:d-mars');
    expect(toMars).toMatchObject({ fromLaneId: 'p-earth', toLaneId: 'p-mars', label: '委派' });
    const first = timeline.handoffs.find((handoff) => handoff.id === 'd:d-earth');
    expect(first).toMatchObject({ fromLaneId: ORIGIN_LANE, kind: 'request' });
  });

  it('shows coordination without inventing a Pi wait from child dispatches', () => {
    const wait = timeline.segments.find((segment) => segment.laneId === 'p-earth' && segment.label.startsWith('协调 '));
    expect(wait?.label).toContain('Mars');
    expect(wait?.label).toContain('Venus');
    expect(collabLaneStateAt(timeline, timeline.lanes.find((lane) => lane.id === 'p-earth')!, demo.nowMs - 50_000).state).toBe('thinking');
  });

  it('records recruitment, tool failure, peer messages, acceptance and the final reply from real receipts', () => {
    expect(timeline.marks.some((mark) => mark.kind === 'recruit' && mark.laneId === 'p-venus')).toBe(true);
    expect(timeline.marks.filter((mark) => mark.kind === 'tool_failed')).toHaveLength(1);
    expect(timeline.handoffs.filter((handoff) => handoff.kind === 'message' || handoff.kind === 'reply')).toHaveLength(2);
    expect(timeline.marks.filter((mark) => mark.kind === 'accept')).toHaveLength(3);
    expect(timeline.final).toBe(true);
    expect(timeline.phases.every((phase) => phase.state === 'done')).toBe(true);
    expect(timeline.lanes.find((lane) => lane.id === 'p-jupiter')?.state).toBe('done');
  });

  it('keeps open work open while the round is live and never invents a finish', () => {
    const mid = collabDemoRoom({ cut: 35 });
    const live = buildRoomCollabTimeline({ room: mid.room, projection: mid.projection, satellites: mid.satellites, nowMs: mid.nowMs });
    expect(live.live).toBe(true);
    expect(live.final).toBe(false);
    expect(live.segments.find((segment) => segment.laneId === 'p-mars')?.open).toBe(true);
    expect(live.lanes.find((lane) => lane.id === 'p-earth')?.state).toBe('thinking');
    expect(live.lanes.find((lane) => lane.id === 'p-jupiter')?.state).toBe('idle');
  });

  it('compresses long idle gaps on the axis but keeps covered work linear', () => {
    const scale = collabTimeScale({ startMs: 0, endMs: 10_000 + COLLAB_GAP_MS * 4, segments: [
      { id: 'a', laneId: 'x', kind: 'execute', startMs: 0, endMs: 10_000, open: false, label: '' },
      { id: 'b', laneId: 'x', kind: 'execute', startMs: 10_000 + COLLAB_GAP_MS * 3, endMs: 10_000 + COLLAB_GAP_MS * 4, open: false, label: '' },
    ], handoffs: [], marks: [] });
    expect(scale.breaks).toHaveLength(1);
    expect(scale.x(10_000)).toBeGreaterThan(0.1);
  });
});

describe('Session Tool Agent timeline projection', () => {
  it('nests child runs under their parent and merges retries into one lane', () => {
    const base = Date.UTC(2026, 8, 29, 9, 0, 0);
    const timeline = buildSessionCollabTimeline({ sessionId: 's', title: '重构', nowMs: base + 60_000, runs: [
      subagentRun({ id: 'r1', parent: 's', task: '规划', template: 'planner', createdAtMs: base, completedAtMs: base + 20_000 }),
      subagentRun({ id: 'r2', parent: 's', task: '执行', template: 'worker', parentRunId: 'r1', depth: 2, createdAtMs: base + 5_000, completedAtMs: base + 12_000, failed: true }),
      subagentRun({ id: 'r2b', nodeId: 'r2', attempt: 2, parent: 's', task: '执行', template: 'worker', parentRunId: 'r1', depth: 2, createdAtMs: base + 13_000, completedAtMs: base + 18_000 }),
    ] });
    expect(timeline.lanes.map((lane) => lane.id)).toEqual([SESSION_LANE, 'run:r1', 'run:r2']);
    expect(timeline.lanes.find((lane) => lane.id === 'run:r2')?.parentId).toBe('run:r1');
    expect(timeline.segments.filter((segment) => segment.laneId === 'run:r2' && segment.kind === 'execute')).toHaveLength(2);
    expect(timeline.handoffs.some((handoff) => handoff.kind === 'return' && handoff.label === '重试')).toBe(true);
    expect(timeline.counts.failed).toBe(1);
  });
});

describe('Collaboration stage', () => {
  it('lists partners, replays to an event, and opens a partner Session', async () => {
    const demo = collabDemoRoom();
    const timeline = buildRoomCollabTimeline({ room: demo.room, projection: demo.projection, satellites: demo.satellites, nowMs: demo.nowMs });
    const onOpen = vi.fn();
    render(<CollabTimelineStage timeline={timeline} active={false} onOpenLane={onOpen} />);
    const stage = screen.getByRole('region', { name: '多 Agent 协作时间线' });
    expect(within(stage).getByRole('button', { name: /Mars，执行伙伴/ })).toBeVisible();
    const feed = screen.getByRole('complementary', { name: '协作事件' });
    expect(within(feed).getByText(/招募 Venus 加入 Room/)).toBeVisible();
    await userEvent.click(within(feed).getByText(/招募 Venus 加入 Room/));
    expect(screen.getByRole('slider', { name: '回放进度' })).not.toHaveValue('1000');
    await userEvent.click(within(stage).getByRole('button', { name: /Jupiter，复核伙伴/ }));
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: 'p-jupiter' }));
  });

  it('summarises who is working in the peek without opening anything', () => {
    const mid = collabDemoRoom({ cut: 20 });
    const timeline = buildRoomCollabTimeline({ room: mid.room, projection: mid.projection, satellites: mid.satellites, nowMs: mid.nowMs });
    render(<CollabTimelinePeek timeline={timeline} active={false} onExpand={vi.fn()} />);
    const peek = screen.getByRole('button', { name: '打开协作全景' });
    expect(peek).toHaveTextContent(/Mars|Venus/);
  });
});

it('shows full task requirements and actual dispatch ownership in Room details', async () => {
  const demo = collabDemoRoom();
  const timeline = buildRoomCollabTimeline({ ...demo });
  expect(timeline.tasks?.find(task => task.id === 'w-mars')?.objective).toBe('写发布说明正文');
  expect(timeline.dispatches?.find(dispatch => dispatch.id === 'd-mars')).toMatchObject({ fromLaneId: 'p-earth', toLaneId: 'p-mars' });
  const task = timeline.tasks!.find(task => task.id === 'w-mars')!;
  task.expectedOutput = '完整发布说明';
  task.acceptance = ['包含迁移步骤'];
  const onOpenLane = vi.fn();
  render(<CollabTimelineStage timeline={timeline} active={false} onOpenLane={onOpenLane} />);
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: '任务与分派' }));
  expect(screen.getByText('具体任务 · 3')).toBeVisible();
  await user.click(screen.getByText('写发布说明正文'));
  expect(screen.getByText('交付：完整发布说明')).toBeVisible();
  expect(screen.getByText('包含迁移步骤')).toBeVisible();
  expect(within(screen.getByText('分派记录 · 4').parentElement!).getAllByText('Earth → Mars').some(element => element.closest('.ctl-tasks'))).toBe(true);
  await user.click(within(screen.getByText('交付：完整发布说明').closest('details')!).getByRole('button', { name: '查看负责人的对话' }));
  expect(onOpenLane).toHaveBeenCalledWith(expect.objectContaining({ id: task.ownerLaneId }));
});
