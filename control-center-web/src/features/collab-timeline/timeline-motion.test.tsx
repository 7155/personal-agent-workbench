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
