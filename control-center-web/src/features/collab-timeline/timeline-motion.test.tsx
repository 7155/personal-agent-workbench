import { act, cleanup, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { CollabTimelineStage } from './CollabTimelineStage';
import { collabDemoRoom } from './fixtures';
import { buildRoomCollabTimeline } from './room-timeline';

vi.mock('@/features/conversation-ui/reading/reading-preferences', () => ({ usePresentationMotion: (active = true) => active }));
afterEach(() => { cleanup(); vi.useRealTimers(); });

it('expires an arrival animation even while the live clock keeps updating the model', () => {
  vi.useFakeTimers();
  const before = collabDemoRoom({ cut: 50 });
  const after = collabDemoRoom({ cut: 54 });
  const initial = buildRoomCollabTimeline({ ...before });
  const arrived = buildRoomCollabTimeline({ ...after });
  const { container, rerender } = render(<CollabTimelineStage timeline={initial} />);
  rerender(<CollabTimelineStage timeline={arrived} />);
  expect(container.querySelectorAll('.ctl-mark[data-pop]').length).toBeGreaterThan(0);
  act(() => { vi.advanceTimersByTime(1000); });
  rerender(<CollabTimelineStage timeline={{ ...arrived, endMs: arrived.endMs + 1000 }} />);
  act(() => { vi.advanceTimersByTime(700); });
  expect(container.querySelectorAll('.ctl-mark[data-pop]')).toHaveLength(0);
});

import { collabFocusPositionAt } from './model';
it('keeps interrupted replay handoffs continuous and deterministic while scrubbing', () => {
  const timeline = buildRoomCollabTimeline({ ...collabDemoRoom({ cut: 80 }) });
  const y = (id: string) => timeline.lanes.findIndex(lane => lane.id === id) * 80;
  for (const handoff of timeline.handoffs) {
    const before = collabFocusPositionAt(timeline, handoff.atMs - 1, y);
    const after = collabFocusPositionAt(timeline, handoff.atMs + 1, y);
    expect(Math.abs(after - before)).toBeLessThan(1);
    expect(collabFocusPositionAt(timeline, handoff.atMs + 1, y)).toBe(after);
  }
});

it('keeps the current tool count visible immediately through frequent receipt updates', () => {
  const timeline = buildRoomCollabTimeline({ ...collabDemoRoom() });
  const { container, rerender } = render(<CollabTimelineStage timeline={timeline} />);
  for (const tools of [98, 99, 100, 101]) {
    rerender(<CollabTimelineStage timeline={{ ...timeline, counts: { ...timeline.counts, tools } }} />);
    const metric = Array.from(container.querySelectorAll('.ctl-kpis > div'))
      .find(node => node.querySelector('dt')?.textContent === '工具调用')!;
    const value = metric.querySelector('dd')!;
    expect(value.textContent).toBe(String(tools));
    expect(Array.from(value.querySelectorAll('*')).some(node => getComputedStyle(node).opacity === '0')).toBe(false);
  }
});
