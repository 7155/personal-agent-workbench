import { useCallback, useLayoutEffect, useMemo, useRef, useState, type RefObject } from 'react';

/* Vendored clean-room virtualizer. See ../ATTRIBUTION.md. */

export interface VirtualRow {
  key: string;
  index: number;
  start: number;
  size: number;
}

export interface UseVirtualTranscriptOptions<T> {
  items: readonly T[];
  getKey(item: T, index: number): string;
  estimateSize(item: T, index: number): number;
  scrollRef: RefObject<HTMLElement | null>;
  overscanTop?: number;
  overscanBottom?: number;
  /** Stable conversation identity used to position one cold-open window. */
  initialScrollKey?: string;
  /** Optional previously captured reader position in the complete list. */
  initialAnchor?: {
    key: string;
    index: number;
    offsetFromViewportTopPx: number;
  };
}

const DEFAULT_UNMEASURED_VIEWPORT_HEIGHT = 800;

/**
 * Small variable-height virtualizer designed for chat timelines.
 * It keeps measured row sizes by stable key and uses asymmetric overscan:
 * substantially more history above, because readers scroll upward.
 */
export function useVirtualTranscript<T>({
  items,
  getKey,
  estimateSize,
  scrollRef,
  overscanTop = 1_400,
  overscanBottom = 600,
  initialScrollKey = '',
  initialAnchor,
}: UseVirtualTranscriptOptions<T>) {
  const sizeCache = useRef(new Map<string, number>());
  const elementToKey = useRef(new WeakMap<Element, string>());
  const measuredElements = useRef(new Map<string, HTMLElement>());
  const measurementRefs = useRef(new Map<string, (element: HTMLElement | null) => void>());
  const observerRef = useRef<ResizeObserver | null>(null);
  const pendingScrollTop = useRef<number | null>(null);
  const layoutRef = useRef<{ rows: VirtualRow[]; totalSize: number }>({ rows: [], totalSize: 0 });
  const initiallyPositionedKeyRef = useRef('');
  const [measurementEpoch, setMeasurementEpoch] = useState(0);
  const [viewport, setViewport] = useState({ top: 0, height: 0 });

  const layout = useMemo(() => {
    const rows: VirtualRow[] = [];
    let start = 0;
    items.forEach((item, index) => {
      const key = getKey(item, index);
      const size = sizeCache.current.get(key) ?? estimateSize(item, index);
      rows.push({ key, index, start, size });
      start += size;
    });
    return { rows, totalSize: start };
  // measurementEpoch intentionally invalidates cached geometry when ResizeObserver reports a row size.
  }, [items, getKey, estimateSize, measurementEpoch]);
  layoutRef.current = layout;

  const initialViewport = useMemo(() => {
    const rows = layout.rows;
    if (rows.length === 0) return { top: 0, height: DEFAULT_UNMEASURED_VIEWPORT_HEIGHT };
    if (!initialAnchor) {
      return {
        top: Math.max(0, layout.totalSize - DEFAULT_UNMEASURED_VIEWPORT_HEIGHT),
        height: DEFAULT_UNMEASURED_VIEWPORT_HEIGHT,
      };
    }
    const exact = rows.find((row) => row.key === initialAnchor.key);
    const anchored = exact ?? rows[Math.max(0, Math.min(initialAnchor.index, rows.length - 1))]!;
    return {
      top: Math.max(0, anchored.start - initialAnchor.offsetFromViewportTopPx),
      height: DEFAULT_UNMEASURED_VIEWPORT_HEIGHT,
    };
  }, [initialAnchor, layout.rows, layout.totalSize]);

  useLayoutEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    let frame = 0;
    const read = () => {
      frame = 0;
      setViewport((previous) => {
        const next = {
          top: scroller.scrollTop,
          height: scroller.clientHeight || DEFAULT_UNMEASURED_VIEWPORT_HEIGHT,
        };
        return previous.top === next.top && previous.height === next.height ? previous : next;
      });
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(read);
    };
    read();
    scroller.addEventListener('scroll', schedule, { passive: true });
    const resize = typeof ResizeObserver === 'function' ? new ResizeObserver(schedule) : null;
    resize?.observe(scroller);
    return () => {
      scroller.removeEventListener('scroll', schedule);
      resize?.disconnect();
      if (frame) cancelAnimationFrame(frame);
    };
  }, [scrollRef]);

  useLayoutEffect(() => {
    const scroller = scrollRef.current;
    if (
      !scroller
      || layout.rows.length === 0
      || initiallyPositionedKeyRef.current === initialScrollKey
    ) return;
    const height = scroller.clientHeight || DEFAULT_UNMEASURED_VIEWPORT_HEIGHT;
    const top = initialAnchor
      ? initialViewport.top
      : Math.max(0, layout.totalSize - height);
    pendingScrollTop.current = null;
    scroller.scrollTop = top;
    initiallyPositionedKeyRef.current = initialScrollKey;
    setViewport({ top, height });
  }, [initialAnchor, initialScrollKey, initialViewport.top, layout.rows.length, layout.totalSize, scrollRef]);

  useLayoutEffect(() => {
    const target = pendingScrollTop.current;
    pendingScrollTop.current = null;
    const scroller = scrollRef.current;
    if (target === null || !scroller) return;
    // Commit the compensating position only after the new sizer height is in
    // the DOM. Assigning it earlier can clamp against the previous height.
    scroller.scrollTop = target;
    setViewport({ top: scroller.scrollTop, height: scroller.clientHeight || DEFAULT_UNMEASURED_VIEWPORT_HEIGHT });
  }, [measurementEpoch, scrollRef]);

  useLayoutEffect(() => {
    if (typeof ResizeObserver !== 'function') return;
    const observer = new ResizeObserver((entries) => {
      let changed = false;
      let scrollAdjustment = 0;
      const scroller = scrollRef.current;
      const geometry = layoutRef.current;
      for (const entry of entries) {
        const key = elementToKey.current.get(entry.target);
        if (!key || measuredElements.current.get(key) !== entry.target) continue;
        const next = Math.ceil(entry.borderBoxSize?.[0]?.blockSize ?? entry.contentRect.height);
        // A removed virtual row or hidden host has no measurable geometry;
        // treating its zero box as a tiny row collapses retained history.
        if (next <= 0) continue;
        const row = geometry.rows.find((candidate) => candidate.key === key);
        const previous = sizeCache.current.get(key) ?? row?.size;
        sizeCache.current.set(key, next);
        if (previous === next) continue;
        // If a fully-above row changes height, compensate scrollTop by the same
        // delta so the first visible content keeps its screen position.
        if (scroller && row && previous !== undefined && row.start + previous <= scroller.scrollTop) {
          scrollAdjustment += next - previous;
        }
        changed = true;
      }
      if (scrollAdjustment && scroller) {
        const top = Math.max(0, (pendingScrollTop.current ?? scroller.scrollTop) + scrollAdjustment);
        pendingScrollTop.current = top;
        // Render the matching window in the same commit as new row geometry,
        // instead of briefly unmounting the reader's row until a scroll RAF.
        setViewport({ top, height: scroller.clientHeight || DEFAULT_UNMEASURED_VIEWPORT_HEIGHT });
      }
      if (changed) setMeasurementEpoch((epoch) => epoch + 1);
    });
    observerRef.current = observer;
    for (const element of measuredElements.current.values()) observer.observe(element);
    return () => {
      observer.disconnect();
      observerRef.current = null;
    };
  }, [scrollRef]);

  const measureElement = useCallback((key: string) => {
    let callback = measurementRefs.current.get(key);
    if (!callback) {
      callback = (element: HTMLElement | null) => {
        const previous = measuredElements.current.get(key);
        if (previous === element) return;
        if (previous) {
          observerRef.current?.unobserve(previous);
          elementToKey.current.delete(previous);
          measuredElements.current.delete(key);
        }
        if (!element) return;
        measuredElements.current.set(key, element);
        elementToKey.current.set(element, key);
        observerRef.current?.observe(element);
      };
      measurementRefs.current.set(key, callback);
    }
    return callback;
  }, []);

  const virtualRows = useMemo(() => {
    const rows = layout.rows;
    if (rows.length === 0) return [];
    // Before refs/layout exist, use the estimated newest or remembered
    // viewport. Rendering every retained row here blocks the very first paint
    // and defeats virtualization for a cold 2,000-event Room snapshot.
    const resolvedViewport = viewport.height > 0 ? viewport : initialViewport;
    const from = Math.max(0, resolvedViewport.top - overscanTop);
    const to = resolvedViewport.top + resolvedViewport.height + overscanBottom;
    let low = 0;
    let high = rows.length - 1;
    let startIndex = rows.length - 1;
    while (low <= high) {
      const mid = (low + high) >> 1;
      const row = rows[mid]!;
      if (row.start + row.size < from) low = mid + 1;
      else {
        startIndex = mid;
        high = mid - 1;
      }
    }

    const visible: VirtualRow[] = [];
    for (let index = startIndex; index < rows.length; index += 1) {
      const row = rows[index]!;
      if (row.start > to) break;
      visible.push(row);
    }
    return visible;
  }, [initialViewport, layout.rows, overscanBottom, overscanTop, viewport]);

  const scrollToIndex = useCallback((index: number, align: 'start' | 'center' | 'end' = 'center') => {
    const scroller = scrollRef.current;
    const row = layout.rows[index];
    if (!scroller || !row) return;
    const top = align === 'start'
      ? row.start
      : align === 'end'
        ? row.start + row.size - scroller.clientHeight
        : row.start + row.size / 2 - scroller.clientHeight / 2;
    scroller.scrollTop = Math.max(0, top);
  }, [layout.rows, scrollRef]);

  return {
    totalSize: layout.totalSize,
    virtualRows,
    measureElement,
    scrollToIndex,
    sizeCache: sizeCache.current,
  };
}
