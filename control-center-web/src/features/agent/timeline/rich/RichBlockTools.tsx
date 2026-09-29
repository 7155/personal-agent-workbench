import { Check, Copy, Download, Maximize2, Minimize2 } from 'lucide-react';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { writeClipboardText } from '@/platform/clipboard';
import { cellsToCsv, cellsToTsv, downloadText } from './rich-data';
import './rich-conversation.css';

export function CopyAction({ value, label = '复制', compact = false }: { value: string | (() => string); label?: string; compact?: boolean }) {
  const [state, setState] = useState<'idle' | 'copied' | 'error'>('idle');
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; clearTimeout(timer.current); }; }, []);
  const title = state === 'copied' ? '已复制' : state === 'error' ? '复制失败，请重试' : label;
  return <>
    <button className="paw-rich-action" type="button" title={title} aria-label={title} data-error={state === 'error' || undefined}
      onClick={async () => {
        clearTimeout(timer.current);
        try { await writeClipboardText(typeof value === 'function' ? value() : value); if (mounted.current) setState('copied'); }
        catch { if (mounted.current) setState('error'); }
        if (mounted.current) timer.current = setTimeout(() => setState('idle'), 2_200);
      }}>
      {state === 'copied' ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />}
      {!compact ? <span>{title}</span> : null}
    </button>
    <span className="paw-rich-sr" role="status">{state === 'idle' ? '' : title}</span>
  </>;
}

export function DownloadAction({ value, fileName, mime }: { value: string | (() => string); fileName: string; mime?: string }) {
  const [error, setError] = useState(false);
  return <button className="paw-rich-action" type="button" title={error ? '保存失败，请重试' : `保存 ${fileName}`} aria-label={error ? '保存失败，请重试' : `保存 ${fileName}`}
    onClick={() => { try { downloadText(typeof value === 'function' ? value() : value, fileName, mime); setError(false); } catch { setError(true); } }}>
    <Download size={14} aria-hidden /><span>{error ? '重试保存' : '保存'}</span>
  </button>;
}

/** Both Markdown and structured tables share this frame. Exports name their visible scope. */
export function RichTableFrame({ children, label = '表格', hint = '可横向滚动', className = '' }: { children: ReactNode; label?: string; hint?: string; className?: string }) {
  const region = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const readRows = (): string[][] => [...(region.current?.querySelectorAll('table tr') ?? [])]
    .map(row => [...row.querySelectorAll(':scope > th, :scope > td')].map(cell => cell.textContent ?? ''));
  return <section className={`paw-rich-table ${className}`} aria-label={label} data-expanded={expanded || undefined}>
    <header className="paw-rich-toolbar"><span className="paw-rich-toolbar__label">{label}<small>{hint}</small></span>
      <span className="paw-rich-toolbar__actions">
        <CopyAction label="复制可见表格" value={() => cellsToTsv(readRows())} compact />
        <button className="paw-rich-action" type="button" aria-label="导出可见行 CSV" title="导出当前显示的行和列；文本公式已转义"
          onClick={() => downloadText('\uFEFF' + cellsToCsv(readRows()), `${label}.csv`, 'text/csv;charset=utf-8')}><Download size={14} aria-hidden /><span>CSV</span></button>
        <button className="paw-rich-action" type="button" aria-label={expanded ? '收起表格高度' : '展开表格高度'} aria-pressed={expanded} onClick={() => setExpanded(!expanded)}>
          {expanded ? <Minimize2 size={14} aria-hidden /> : <Maximize2 size={14} aria-hidden />}
        </button>
      </span>
    </header>
    <div className="paw-rich-table__scroll" ref={region} role="region" aria-label={`${label}可滚动内容`} tabIndex={0}>{children}</div>
  </section>;
}
