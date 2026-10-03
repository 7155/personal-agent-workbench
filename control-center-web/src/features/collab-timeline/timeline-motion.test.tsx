import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { MotionActivityBoundary, MotionProvider, useMotionPreference } from '@/design/motion';
import { updateReadingPreferences } from '@/features/conversation-ui/reading/reading-preferences';
import { CollabTimelineStage } from './CollabTimelineStage';
import { collabDemoRoom } from '@/test/fixtures/collab-timeline';
import { buildRoomCollabTimeline } from './room-timeline';

beforeEach(() => {
  localStorage.clear();
  updateReadingPreferences({ motion: 'system' });
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
  vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  delete document.documentElement.dataset.reduceMotion;
  delete document.documentElement.dataset.motionPreference;
});

function ReduceMotionControl() {
  const { setPreference } = useMotionPreference();
  return <button onClick={() => setPreference('reduce')}>减少动态效果</button>;
}

it('disables automatic replay when the user reduces motion in the shared appearance settings', async () => {
  const timeline = buildRoomCollabTimeline({ ...collabDemoRoom({ cut: 35 }) });
  render(<MotionProvider><ReduceMotionControl /><CollabTimelineStage timeline={timeline} /></MotionProvider>);
  const replay = screen.getByRole('button', { name: '从头回放' });
  expect(replay).toBeEnabled();
  await userEvent.click(screen.getByRole('button', { name: '减少动态效果' }));
  expect(replay).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: '上一条事件' }));
  expect(Number(screen.getByRole('slider', { name: '回放进度' }).getAttribute('value'))).toBeLessThan(1000);
});

it('pauses decorative replay in an inactive host while current tool receipts keep updating', () => {
  const timeline = buildRoomCollabTimeline({ ...collabDemoRoom() });
  const view = (tools: number) => <MotionProvider><MotionActivityBoundary active={false}>
    <CollabTimelineStage timeline={{ ...timeline, counts: { ...timeline.counts, tools } }} />
  </MotionActivityBoundary></MotionProvider>;
  const { container, rerender } = render(view(100));
  expect(screen.getByRole('button', { name: '从头回放' })).toBeDisabled();
  rerender(view(101));
  const metric = Array.from(container.querySelectorAll('.ctl-kpis > div'))
    .find(node => node.querySelector('dt')?.textContent === '工具调用')!;
  expect(metric.querySelector('dd')).toHaveTextContent('101');
});

it('retains the reading surface motion preference without disabling manual event inspection', async () => {
  updateReadingPreferences({ motion: 'reduced' });
  const timeline = buildRoomCollabTimeline({ ...collabDemoRoom({ cut: 35 }) });
  render(<MotionProvider><CollabTimelineStage timeline={timeline} /></MotionProvider>);
  expect(screen.getByRole('button', { name: '从头回放' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: '上一条事件' }));
  expect(Number(screen.getByRole('slider', { name: '回放进度' }).getAttribute('value'))).toBeLessThan(1000);
});

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
