import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useVirtualTranscript } from './useVirtualTranscript';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

class ControlledResizeObserver {
  static instances: ControlledResizeObserver[] = [];
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
  constructor(private callback: ResizeObserverCallback) { ControlledResizeObserver.instances.push(this); }
  emit(target: Element, height: number) {
    const box = { blockSize: height, inlineSize: 100 };
    this.callback([{
      target, contentRect: new DOMRect(0, 0, 100, height),
      borderBoxSize: [box], contentBoxSize: [box], devicePixelContentBoxSize: [box],
    }], this as unknown as ResizeObserver);
  }
}

function measuredTranscript() {
  ControlledResizeObserver.instances = [];
  vi.stubGlobal('ResizeObserver', ControlledResizeObserver);
  const scroller = document.createElement('div');
  Object.defineProperty(scroller, 'clientHeight', { value: 200 });
  const items = Array.from({ length: 100 }, (_, index) => ({ id: `message-${index}` }));
  const scrollRef = { current: scroller };
  const hook = renderHook(() => useVirtualTranscript({
    items, getKey: (item) => item.id, estimateSize: () => 100, scrollRef,
    initialScrollKey: 'room:read', initialAnchor: { key: 'message-50', index: 50, offsetFromViewportTopPx: 0 },
    overscanTop: 0, overscanBottom: 0,
  }));
  return { ...hook, scroller, observer: ControlledResizeObserver.instances.at(-1)! };
}

describe('useVirtualTranscript cold-open window', () => {
  it('mounts the remembered full-transcript index before a scroller can be measured', () => {
    const items = Array.from({ length: 100 }, (_value, index) => ({
      id: `message-${index}`,
    }));
    const scrollRef = { current: null };
    const { result } = renderHook(() => useVirtualTranscript({
      items,
      getKey: (item) => item.id,
      estimateSize: () => 100,
      scrollRef,
      initialScrollKey: 'room:remembered',
      initialAnchor: {
        key: 'message-50',
        index: 50,
        offsetFromViewportTopPx: 0,
      },
    }));

    const visibleIndexes = result.current.virtualRows.map((row) => row.index);
    expect(visibleIndexes).toContain(50);
    expect(visibleIndexes[0]).toBeGreaterThan(0);
    expect(visibleIndexes.at(-1)).toBeLessThan(99);
  });
});

describe('useVirtualTranscript measured reader position', () => {
  it('unobserves virtual rows on removal and ignores late detached size notifications', () => {
    const { result, scroller, observer } = measuredTranscript();
    const row = document.createElement('div');
    const ref = result.current.measureElement('message-40');
    act(() => { ref(row); observer.emit(row, 100); });
    act(() => { ref(null); observer.emit(row, 0); });
    expect(observer.unobserve).toHaveBeenCalledWith(row);
    expect(result.current.totalSize).toBe(10000);
    expect(scroller.scrollTop).toBe(5000);
  });

  it('keeps the same measurement ref across streaming rerenders', () => {
    const { result, rerender } = measuredTranscript();
    const ref = result.current.measureElement('message-50');
    rerender();
    expect(result.current.measureElement('message-50')).toBe(ref);
  });

  it('keeps the visible row mounted in the same commit as an above-viewport correction', () => {
    const { result, scroller, observer } = measuredTranscript();
    const row = document.createElement('div');
    result.current.measureElement('message-40')(row);
    act(() => observer.emit(row, 500));
    expect(scroller.scrollTop).toBe(5400);
    expect(result.current.virtualRows.map((item) => item.key)).toContain('message-50');
    const anchor = result.current.virtualRows.find((item) => item.key === 'message-50')!;
    expect(anchor.start - scroller.scrollTop).toBe(0);
  });

  it('does not shrink cached row geometry when its host is temporarily hidden', () => {
    const { result, scroller, observer } = measuredTranscript();
    const row = document.createElement('div');
    result.current.measureElement('message-40')(row);
    act(() => observer.emit(row, 120));
    act(() => observer.emit(row, 0));
    expect(result.current.sizeCache.get('message-40')).toBe(120);
    expect(scroller.scrollTop).toBe(5020);
  });

  it('waits for the new sizer height before applying a correction near the old bottom', () => {
    ControlledResizeObserver.instances = [];
    vi.stubGlobal('ResizeObserver', ControlledResizeObserver);
    let renderedHeight = 10000;
    let top = 0;
    const scroller = document.createElement('div');
    Object.defineProperties(scroller, {
      clientHeight: { value: 200 },
      scrollTop: {
        get: () => top,
        set: (value: number) => { top = Math.max(0, Math.min(value, renderedHeight - 200)); },
      },
    });
    const scrollRef = { current: scroller };
    const items = Array.from({ length: 100 }, (_, index) => ({ id: `message-${index}` }));
    const { result } = renderHook(() => {
      const virtual = useVirtualTranscript({
        items, getKey: (item) => item.id, estimateSize: () => 100, scrollRef,
        initialScrollKey: 'room:bottom', initialAnchor: { key: 'message-98', index: 98, offsetFromViewportTopPx: 0 },
      });
      // Model the sizer's rendered height; a scroll write before React renders
      // the new geometry is clamped to the previous browser scroll range.
      renderedHeight = virtual.totalSize;
      return virtual;
    });
    const row = document.createElement('div');
    result.current.measureElement('message-90')(row);
    act(() => ControlledResizeObserver.instances.at(-1)!.emit(row, 500));
    expect(scroller.scrollTop).toBe(10200);
    expect(result.current.totalSize).toBe(10400);
  });
});
