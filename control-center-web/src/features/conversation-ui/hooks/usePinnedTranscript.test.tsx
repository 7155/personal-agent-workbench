import { act, cleanup, fireEvent, renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { usePinnedTranscript } from './usePinnedTranscript';
import { useVirtualTranscript } from './useVirtualTranscript';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function pinnedTranscript() {
  let resize: ResizeObserverCallback = () => {};
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: ResizeObserverCallback) { resize = callback; }
    observe() {}
    disconnect() {}
  });
  const frames = new Map<number, FrameRequestCallback>();
  let frameId = 0;
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { frames.set(++frameId, callback); return frameId; });
  vi.stubGlobal('cancelAnimationFrame', (id: number) => { frames.delete(id); });
  const flushFrame = () => {
    const pending = [...frames.values()]; frames.clear();
    for (const callback of pending) callback(0);
  };
  const scroller = document.createElement('div');
  const content = document.createElement('div');
  let height = 1000;
  let top = 800;
  Object.defineProperties(scroller, {
    clientHeight: { get: () => 200 },
    scrollHeight: { get: () => height },
    scrollTop: { get: () => top, set: (value: number) => { top = Math.max(0, Math.min(value, height - 200)); } },
  });
  const scrollRef = { current: scroller };
  const contentRef = { current: content };
  const hook = renderHook(({ id }) => usePinnedTranscript(scrollRef, contentRef, id), { initialProps: { id: 'room:one' } });
  return { ...hook, scroller, frames, flushFrame,
    grow() { height += 200; resize([], {} as ResizeObserver); },
    resizeBy(delta: number) { height += delta; resize([], {} as ResizeObserver); },
  };
}

it('does not run an already scheduled bottom-follow after the reader scrolls away', () => {
  const { result, scroller, grow, flushFrame } = pinnedTranscript();
  act(grow);
  act(() => { scroller.scrollTop = 500; fireEvent.scroll(scroller); });
  expect(result.current.isPinnedRef.current).toBe(false);
  act(flushFrame);
  expect(scroller.scrollTop).toBe(500);
  expect(result.current.isPinnedRef.current).toBe(false);
});

it('keeps bottom-follow working when the reader remains pinned', () => {
  const { result, scroller, grow, flushFrame } = pinnedTranscript();
  act(grow);
  act(flushFrame);
  expect(scroller.scrollTop).toBe(1000);
  expect(result.current.isPinnedRef.current).toBe(true);
});

it('releases the pin on upward wheel intent before the browser scroll event', () => {
  const { result, scroller } = pinnedTranscript();
  act(() => fireEvent.wheel(scroller, { deltaY: -100 }));
  expect(result.current.isPinnedRef.current).toBe(false);
});

it('lets upward user input interrupt a smooth jump before a resize follow frame', () => {
  const { result, scroller, grow, flushFrame } = pinnedTranscript();
  scroller.scrollTo = vi.fn();
  act(() => { scroller.scrollTop = 300; fireEvent.scroll(scroller); });
  act(() => result.current.scrollToBottom('smooth'));
  act(grow);
  act(() => { scroller.scrollTop = 500; fireEvent.scroll(scroller); });
  expect(result.current.isPinnedRef.current).toBe(true);
  act(() => { fireEvent.wheel(scroller, { deltaY: -100 }); scroller.scrollTop = 400; fireEvent.scroll(scroller); });
  act(flushFrame);
  expect(scroller.scrollTop).toBe(400);
  expect(result.current.isPinnedRef.current).toBe(false);
  expect(result.current.showJumpToBottom).toBe(true);
});

it('keeps the pin through an above-row shrink correction while the tail is growing', () => {
  const { result, scroller, grow, resizeBy, flushFrame } = pinnedTranscript();
  act(grow);
  act(() => fireEvent.scroll(scroller));
  act(() => { resizeBy(-100); scroller.scrollTop -= 100; fireEvent.scroll(scroller); });
  expect(result.current.isPinnedRef.current).toBe(true);
  act(flushFrame);
  expect(scroller.scrollTop).toBe(900);
  expect(result.current.isPinnedRef.current).toBe(true);
});

it('cancels a delayed anchor restore when the reader takes control', () => {
  const { result, scroller, flushFrame } = pinnedTranscript();
  const row = document.createElement('div');
  row.dataset.messageId = 'message:anchor';
  row.dataset.index = '5';
  row.getBoundingClientRect = () => ({ top: 500 - scroller.scrollTop, height: 100 }) as DOMRect;
  scroller.append(row);
  act(() => result.current.restoreAnchor({ pinned: false, row: {
    conversationId: 'room:one', rowKey: 'message:anchor', rowIndex: 5, offsetFromViewportTopPx: 0, fallbackScrollTop: 500,
  } }));
  expect(scroller.scrollTop).toBe(500);
  act(() => { fireEvent.wheel(scroller, { deltaY: -100 }); scroller.scrollTop = 300; fireEvent.scroll(scroller); });
  act(flushFrame);
  act(flushFrame);
  expect(scroller.scrollTop).toBe(300);
});

it.each(['switch', 'unmount'])('cancels a delayed restore on conversation %s', (action) => {
  const { result, scroller, flushFrame, rerender, unmount } = pinnedTranscript();
  act(() => result.current.restoreAnchor({ pinned: false, row: {
    conversationId: 'room:one', rowKey: 'message:missing', rowIndex: 5,
    offsetFromViewportTopPx: 0, fallbackScrollTop: 500,
  } }));
  if (action === 'switch') rerender({ id: 'room:two' });
  else unmount();
  scroller.scrollTop = 200;
  act(flushFrame);
  act(flushFrame);
  expect(scroller.scrollTop).toBe(200);
});

it('finishes an explicit jump when the last virtual row grows after reaching the estimated bottom', () => {
  const observers: { callback: ResizeObserverCallback; targets: Set<Element> }[] = [];
  vi.stubGlobal('ResizeObserver', class {
    record: typeof observers[number];
    constructor(callback: ResizeObserverCallback) { this.record = { callback, targets: new Set() }; observers.push(this.record); }
    observe(target: Element) { this.record.targets.add(target); }
    unobserve(target: Element) { this.record.targets.delete(target); }
    disconnect() {}
  });
  const frames = new Map<number, FrameRequestCallback>();
  let id = 0;
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { frames.set(++id, callback); return id; });
  vi.stubGlobal('cancelAnimationFrame', (key: number) => frames.delete(key));
  const flush = () => { const pending = [...frames.values()]; frames.clear(); pending.forEach(callback => callback(0)); };
  const scroller = document.createElement('div');
  const content = document.createElement('div');
  let height = 10000;
  let top = 0;
  Object.defineProperties(scroller, {
    clientHeight: { value: 429 }, scrollHeight: { get: () => height },
    scrollTop: { get: () => top, set: (value: number) => { top = Math.max(0, Math.min(value, height - 429)); } },
  });
  scroller.scrollTo = vi.fn(); // Model the animation's delivered scroll frames below.
  const scrollRef = { current: scroller }, contentRef = { current: content };
  const items = Array.from({ length: 100 }, (_, index) => ({ id: `message-${index}` }));
  const { result } = renderHook(() => {
    const pinned = usePinnedTranscript(scrollRef, contentRef, 'room:long');
    const virtual = useVirtualTranscript({ items, getKey: item => item.id, estimateSize: () => 100,
      scrollRef, initialScrollKey: 'room:long',
      initialAnchor: { key: 'message-50', index: 50, offsetFromViewportTopPx: 0 } });
    height = virtual.totalSize;
    return { pinned, virtual };
  });
  expect(result.current.pinned.isPinned).toBe(false);
  act(() => result.current.pinned.scrollToBottom('smooth'));
  act(() => { scroller.scrollTop = height; fireEvent.scroll(scroller); flush(); });
  const last = document.createElement('div');
  result.current.virtual.measureElement('message-99')(last);
  const observer = observers.find(item => item.targets.has(last))!;
  const box = { blockSize: 1077, inlineSize: 100 };
  act(() => observer.callback([{ target: last, contentRect: new DOMRect(0, 0, 100, 1077),
    borderBoxSize: [box], contentBoxSize: [box], devicePixelContentBoxSize: [box] }], {} as ResizeObserver));
  expect(scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop).toBe(977);
  // The delayed native scroll event from the jump sees the newly committed
  // height before the content ResizeObserver's follow-to-bottom frame.
  act(() => { fireEvent.scroll(scroller); fireEvent.scroll(scroller); });
  act(() => observers.find(item => item.targets.has(content))!.callback([], {} as ResizeObserver));
  act(flush);
  expect(scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop).toBe(0);
  expect(result.current.pinned.isPinnedRef.current).toBe(true);
});
