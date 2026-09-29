import { ArrowLeft, ArrowRight, Check, Copy, Download, LoaderCircle, Maximize2, Minimize2 } from 'lucide-react';
import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { writeClipboardText } from '@/platform/clipboard';
import { usePresentationMotion } from '../../../conversation-ui/reading/reading-preferences';
import { cellsToCsv, cellsToTsv, downloadText } from './rich-data';
import './rich-conversation.css';

type ActionState = 'idle' | 'pending' | 'done' | 'error';
function useActionFeedback() {
  const [state, setState] = useState<ActionState>('idle');
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const mounted = useRef(false);
  const busy = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; clearTimeout(timer.current); };
  }, []);
  async function run(action: () => Promise<unknown> | unknown) {
    if (busy.current) return;
    busy.current = true;
    clearTimeout(timer.current);
    setState('pending');
    try { await action(); if (mounted.current) setState('done'); }
    catch { if (mounted.current) setState('error'); }
    finally {
      busy.current = false;
      if (mounted.current) timer.current = setTimeout(() => setState('idle'), 2400);
    }
  }
  return { state, run };
}

export function CopyAction({ value, label = '复制', compact = false }: {
  value: string | (() => string); label?: string; compact?: boolean;
}) {
  const { state, run } = useActionFeedback();
  const motion = usePresentationMotion();
  const title = state === 'done' ? '已复制' : state === 'error' ? '复制失败，请重试' : state === 'pending' ? '正在复制' : label;
  return <>
    <button className="paw-rich-action" type="button" title={title} aria-label={title}
      disabled={state === 'pending'} aria-busy={state === 'pending'} data-feedback={state}
      data-motion={motion ? 'active' : 'paused'} data-error={state === 'error' || undefined}
      onClick={() => void run(() => writeClipboardText(typeof value === 'function' ? value() : value))}>
      {state === 'done' ? <Check size={14} aria-hidden /> : state === 'pending'
        ? <LoaderCircle size={14} aria-hidden className="paw-rich-action__spin" /> : <Copy size={14} aria-hidden />}
      {!compact ? <span>{title}</span> : null}
    </button>
    <span className="paw-rich-sr" role="status">{state === 'done' || state === 'error' ? title : ''}</span>
  </>;
}

export function DownloadAction({ value, fileName, mime, label = '保存', title }: {
  value: string | (() => string); fileName: string; mime?: string; label?: string; title?: string;
}) {
  const { state, run } = useActionFeedback();
  const motion = usePresentationMotion();
  const accessible = state === 'error' ? '下载未能发起，请重试' : state === 'done' ? '已发起下载' : title || `保存 ${fileName}`;
  return <>
    <button className="paw-rich-action" type="button" title={accessible} aria-label={accessible}
      disabled={state === 'pending'} data-feedback={state} data-error={state === 'error' || undefined}
      data-motion={motion ? 'active' : 'paused'}
      onClick={() => void run(() => downloadText(typeof value === 'function' ? value() : value, fileName, mime))}>
      {state === 'done' ? <Check size={14} aria-hidden /> : <Download size={14} aria-hidden />}
      <span>{state === 'error' ? '重试' : state === 'done' ? '已发起' : label}</span>
    </button>
    <span className="paw-rich-sr" role="status">{state === 'done' || state === 'error' ? accessible : ''}</span>
  </>;
}

/** Exports come from displayed cells; overflow hints describe measured layout. */
export function RichTableFrame({ children, label = '表格', hint, className = '' }: {
  children: ReactNode; label?: string; hint?: string; className?: string;
}) {
  const id = useId();
  const region = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [geometry, setGeometry] = useState({ left: false, right: false, rows: 0, columns: 0 });
  const motion = usePresentationMotion();
  useEffect(() => {
    const node = region.current;
    if (!node) return;
    let frame = 0;
    const measure = () => {
      frame = 0;
      const allRows = node.querySelectorAll('table tr');
      const next = {
        left: node.scrollLeft > 2,
        right: node.scrollWidth - node.clientWidth - node.scrollLeft > 2,
        rows: Math.max(0, allRows.length - node.querySelectorAll('thead tr').length),
        columns: allRows[0]?.querySelectorAll(':scope > th,:scope > td').length ?? 0,
      };
      setGeometry(previous => previous.left === next.left && previous.right === next.right
        && previous.rows === next.rows && previous.columns === next.columns ? previous : next);
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(measure); };
    const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule);
    const mutation = new MutationObserver(schedule);
    resize?.observe(node);
    const table = node.querySelector('table');
    if (table) resize?.observe(table);
    mutation.observe(node, { childList: true, subtree: true, characterData: true });
    node.addEventListener('scroll', schedule, { passive: true });
    measure();
    return () => { cancelAnimationFrame(frame); resize?.disconnect(); mutation.disconnect(); node.removeEventListener('scroll', schedule); };
  }, [children]);
  const readRows = (): string[][] => [...(region.current?.querySelectorAll('table tr') ?? [])]
    .map(row => [...row.querySelectorAll(':scope > th, :scope > td')].map(cell => cell.textContent ?? ''));
  const move = (direction: number) => {
    const node = region.current;
    node?.scrollBy({ left: direction * node.clientWidth * .75, behavior: motion ? 'smooth' : 'auto' });
  };
  return <section className={`paw-rich-table ${className}`} aria-label={label}
    data-expanded={expanded || undefined} data-overflow-left={geometry.left || undefined} data-overflow-right={geometry.right || undefined}>
    <header className="paw-rich-toolbar"><span className="paw-rich-toolbar__label"><span>{label}</span>
      <small>{hint || `${geometry.rows} 行 · ${geometry.columns} 列`}</small></span>
      <span className="paw-rich-toolbar__actions">
        <CopyAction label="复制当前表格" value={() => cellsToTsv(readRows())} compact />
        <DownloadAction value={() => '\uFEFF' + cellsToCsv(readRows())} fileName={`${label}.csv`}
          mime="text/csv;charset=utf-8" label="CSV" title="导出当前显示的行和列；文本公式已转义" />
        <button className="paw-rich-action" type="button" aria-label={expanded ? '收起表格高度' : '展开表格高度'}
          aria-pressed={expanded} onClick={() => setExpanded(!expanded)}>
          {expanded ? <Minimize2 size={14} aria-hidden /> : <Maximize2 size={14} aria-hidden />}
        </button>
      </span>
    </header>
    <div className="paw-rich-table__viewport">
      <div className="paw-rich-table__scroll" ref={region} role="region" aria-label={`${label}可滚动内容`}
        aria-describedby={`${id}-scope`} tabIndex={0}>{children}</div>
    </div>
    <footer className="paw-rich-table__caption" id={`${id}-scope`}>
      <span>复制与导出仅包含当前显示的数据</span>
      {geometry.left || geometry.right ? <span className="paw-rich-table__navigate">
        <small>还有列可查看</small>
        <button type="button" className="paw-rich-action" aria-label="向左查看表格" disabled={!geometry.left} onClick={() => move(-1)}><ArrowLeft size={13} aria-hidden /></button>
        <button type="button" className="paw-rich-action" aria-label="向右查看表格" disabled={!geometry.right} onClick={() => move(1)}><ArrowRight size={13} aria-hidden /></button>
      </span> : null}
    </footer>
  </section>;
}
