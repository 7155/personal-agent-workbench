import { useEffect, useId, useRef, useState } from 'react';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/primitives';
import { usePresentationMotion } from '../reading/reading-preferences';
import { ImageGalleryView, ImageViewerView } from './ImageGalleryView';
import { imageKey, limitZoom, type GalleryImage, type ImageLoadMap, type ImageViewerMode } from './image-gallery-model';

/** Shared reader for managed message media and user-selected local draft bytes.
 * No fetch/dispatch/install command lives in this presentation component.
 */
export function ImageGallery({ items, compact = false, onOpenOriginal }: {
  items: readonly GalleryImage[]; compact?: boolean; onOpenOriginal?: (image: GalleryImage) => void;
}) {
  const id = useId(); const root = useRef<HTMLSpanElement>(null); const origin = useRef<HTMLElement | null>(null);
  const [currentId, setCurrentId] = useState<string | null>(null); const [mode, setMode] = useState<ImageViewerMode>('single');
  const [layout, setLayout] = useState<'grid' | 'focus'>(() => !compact && items.length > 2 ? 'focus' : 'grid');
  const [focusId, setFocusId] = useState(items[0]?.id ?? '');
  const [backdrop, setBackdrop] = useState<'paper' | 'ink' | 'checker'>('paper');
  const [zoom, setZoom] = useState(0); const [compare, setCompare] = useState<[string, string]>(['', '']);
  const [limit, setLimit] = useState(6); const [loads, setLoads] = useState<ImageLoadMap>({});
  const [retries, setRetries] = useState<Record<string, number>>({});
  const motion = usePresentationMotion(); const current = items.find(item => item.id === currentId);
  const signature = JSON.stringify(items.map(imageKey));
  useEffect(() => {
    const keys = new Set<string>(JSON.parse(signature));
    setLoads(previous => { const next = Object.fromEntries(Object.entries(previous).filter(([key]) => keys.has(key))); return Object.keys(next).length === Object.keys(previous).length ? previous : next; });
    setRetries(previous => { const next = Object.fromEntries(Object.entries(previous).filter(([key]) => keys.has(key))); return Object.keys(next).length === Object.keys(previous).length ? previous : next; });
  }, [signature]);
  useEffect(() => {
    if (currentId && !items.some(item => item.id === currentId)) setCurrentId(null);
  }, [signature, currentId, items]);
  function load(image: GalleryImage, width: number, height: number) {
    const key = imageKey(image);
    setLoads(previous => previous[key]?.state === 'loaded' && previous[key]?.width === width && previous[key]?.height === height
      ? previous : { ...previous, [key]: { state: 'loaded', width, height } });
  }
  function error(image: GalleryImage) {
    const key = imageKey(image); setLoads(previous => previous[key]?.state === 'failed' ? previous : { ...previous, [key]: { state: 'failed' } });
  }
  function retry(image: GalleryImage) {
    const key = imageKey(image);
    setLoads(previous => { const next = { ...previous }; delete next[key]; return next; });
    setRetries(previous => ({ ...previous, [key]: (previous[key] || 0) + 1 }));
  }
  function choose(value: string) { setCurrentId(value); setZoom(0); }
  function open(value: string) {
    origin.current = typeof document !== 'undefined' && document.activeElement instanceof HTMLElement ? document.activeElement : null;
    choose(value); setMode('single');
    setCompare([value, items.find(item => item.id !== value)?.id ?? value]);
  }
  const common = { items, loads, retries, motion, onLoad: load, onError: error, onRetry: retry };
  if (!items.length) return null;
  return <Dialog open={Boolean(current)} onOpenChange={value => { if (!value) setCurrentId(null); }}>
    <span className="paw-image-gallery-host" ref={root} data-compact={compact}>
      <ImageGalleryView {...common} layout={layout} onLayout={setLayout} focusId={focusId} onFocus={setFocusId} compact={compact} limit={limit} onMore={() => setLimit(value => Math.min(items.length, value + 12))} onOpen={open} />
      <DialogContent className="paw-image-dialog" aria-describedby={`${id}-description`} onMouseDown={event => event.stopPropagation()}
        onOpenAutoFocus={event => {
          event.preventDefault(); requestAnimationFrame(() => document.getElementById(`${id}-viewer`)?.querySelector<HTMLElement>('[aria-label="可滚动的图片画面"]')?.focus({ preventScroll: true }));
        }} onCloseAutoFocus={event => { event.preventDefault(); const target = origin.current; if (target?.isConnected) target.focus({ preventScroll: true }); else root.current?.querySelector<HTMLButtonElement>('button')?.focus({ preventScroll: true }); }}>
        <DialogTitle>{current?.name || '图片'}</DialogTitle><DialogDescription id={`${id}-description`} className="paw-image-dialog__description">左右键切图 · 0 适应画面 · 1 原尺寸 · Escape 返回对话</DialogDescription>
        <div id={`${id}-viewer`}>{current ? <ImageViewerView {...common} currentId={current.id} mode={mode} compareIds={compare} zoom={zoom}
          backdrop={backdrop} onBackdrop={setBackdrop} onCurrent={choose} onMode={setMode} onZoom={value => setZoom(value === 0 ? 0 : limitZoom(value))}
          onCompare={(slot, value) => setCompare(previous => {
            const next: [string, string] = [...previous]; const other = slot === 0 ? 1 : 0;
            if (next[other] === value) next[other] = next[slot]; next[slot] = value; return next;
          })} onOriginal={onOpenOriginal} /> : null}</div>
      </DialogContent>
    </span>
  </Dialog>;
}
