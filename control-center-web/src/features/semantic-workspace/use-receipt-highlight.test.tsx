import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useReceiptHighlight } from './use-receipt-highlight';

afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('receipt highlight', () => {
  it('ignores initial and historical snapshots and expires newly observed receipts on time', () => {
    vi.useFakeTimers();
    const view = renderHook(({ scope, keys, observing }) => useReceiptHighlight(scope, keys, observing, 900), {
      initialProps: { scope: 'graph', keys: ['old'], observing: true },
    });
    expect(view.result.current.size).toBe(0);
    view.rerender({ scope: 'graph', keys: ['old', 'new'], observing: true });
    expect([...view.result.current]).toEqual(['new']);
    act(() => vi.advanceTimersByTime(500));
    view.rerender({ scope: 'graph', keys: ['old', 'new'], observing: true });
    act(() => vi.advanceTimersByTime(400));
    expect(view.result.current.size).toBe(0);
    view.rerender({ scope: 'history', keys: ['old', 'new'], observing: false });
    expect(view.result.current.size).toBe(0);
    view.rerender({ scope: 'history', keys: ['old', 'new'], observing: true });
    expect(view.result.current.size).toBe(0);
  });
});
