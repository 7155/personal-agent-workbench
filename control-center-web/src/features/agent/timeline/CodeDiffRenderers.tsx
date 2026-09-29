import { ChevronRight, Code2, FileDiff, ListOrdered, Maximize2, Minimize2, WrapText } from 'lucide-react';
import { useEffect, useId, useMemo, useState } from 'react';
import { Disclosure } from '@/components/primitives';
import { DiffPreview } from '../file-preview/DiffPreview';
import { highlightCode } from '../file-preview/syntax-highlighter';
import type { AgentBlockRenderProps } from './renderer-contract';
import { text } from './renderer-values';
import { CopyAction, DownloadAction } from './rich/RichBlockTools';
import { RichCodePreview } from './rich/RichCodePreview';
import { fencedPreviewKind } from './rich/rich-data';
import './rich/rich-conversation.css';
import { usePresentationMotion } from '../../conversation-ui/reading/reading-preferences';

const HIGHLIGHT_CHAR_LIMIT = 60_000;

export function CodeBlockRenderer({ block }: AgentBlockRenderProps) {
  return <CodeContentBlock code={text(block.data.code ?? block.data.text)} language={text(block.data.language) || 'text'}
    fileName={text(block.data.fileName ?? block.data.title)} streamingTail={block.status === 'running'} />;
}

export function DiffBlockRenderer({ block }: AgentBlockRenderProps) {
  const content = text(block.data.diff ?? block.data.text);
  const title = text(block.data.fileName ?? block.data.title) || '代码变更';
  const lineCount = content ? content.split('\n').length : 0;
  return <Disclosure className="agent-inline-diff agent-rich-collapsible paw-rich-diff" data-tone="project" defaultOpen={lineCount <= 80}
    summary={<><span className="agent-insert-icon"><FileDiff size={15} /></span><span>{title}</span><small>{lineCount} 行</small><ChevronRight className="agent-rich-collapsible__chevron" size={14} /></>}>
    <DiffPreview content={content} disclosureRegionLabel={`${title}完整变更`} fileName={title} />
  </Disclosure>;
}

export function CodeContentBlock({ code, language, fileName, streamingTail = false }: {
  code: string; language: string; fileName?: string; streamingTail?: boolean;
}) {
  const id = useId();
  const motionAllowed = usePresentationMotion();
  const kind = fencedPreviewKind(language);
  const [view, setView] = useState<'source' | 'preview'>(() => kind && kind !== 'json' && !streamingTail ? 'preview' : 'source');
  const [wrap, setWrap] = useState(false);
  const [numbers, setNumbers] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [highlighted, setHighlighted] = useState<{ code: string; language: string; html: string } | null>(null);
  const lines = useMemo(() => code.split('\n'), [code]);
  const lineCount = code ? lines.length : 0;
  const long = lineCount > 24 || code.length > 4_000;
  const showPreview = Boolean(kind && view === 'preview' && !streamingTail);
  const highlightable = !showPreview && !streamingTail && Boolean(code) && code.length <= HIGHLIGHT_CHAR_LIMIT;
  const downloadName = fileName || `snippet.${({ typescript: 'ts', javascript: 'js', python: 'py', bash: 'sh', text: 'txt', math: 'tex', latex: 'tex' } as Record<string, string>)[language] || language.replace(/[^a-z0-9]/giu, '') || 'txt'}`;
  useEffect(() => {
    if (!highlightable) return;
    let current = true;
    void highlightCode(code, language, { inheritSurface: true })
      .then(html => { if (current) setHighlighted({ code, language, html }); })
      .catch(() => { if (current) setHighlighted(null); });
    return () => { current = false; };
  }, [code, language, highlightable]);
  const html = highlightable && highlighted?.code === code && highlighted.language === language ? highlighted.html : '';
  return <figure className="agent-code-block paw-rich-code" data-streaming={streamingTail || undefined} data-wrap={wrap || undefined}
    data-reduce-motion={!motionAllowed || undefined} data-line-numbers={numbers || undefined} data-expanded={expanded || undefined} data-long={long || undefined}>
    <figcaption className="paw-rich-toolbar">
      <span className="paw-rich-toolbar__label"><Code2 size={14} aria-hidden /><span title={fileName}>{fileName || language}</span><small>{streamingTail ? '正在生成' : `${lineCount} 行`}</small></span>
      <div className="paw-rich-toolbar__actions">
        {!showPreview ? <details className="paw-rich-code__options"
          onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); event.currentTarget.open = false; event.currentTarget.querySelector('summary')?.focus(); } }}
          onBlur={event => { if (event.relatedTarget instanceof Node && !event.currentTarget.contains(event.relatedTarget)) event.currentTarget.open = false; }}>
          <summary title="代码显示选项"><WrapText size={14} aria-hidden /><span>显示</span></summary>
          <div role="group" aria-label="代码显示选项">
            <button type="button" aria-pressed={wrap} onClick={() => setWrap(!wrap)}><WrapText size={14} aria-hidden /><span>自动换行</span><small>{wrap ? '开' : '关'}</small></button>
            <button type="button" aria-pressed={numbers} disabled={streamingTail || lines.length > 2_000} onClick={() => setNumbers(!numbers)}><ListOrdered size={14} aria-hidden /><span>行号</span><small>{numbers ? '开' : '关'}</small></button>
          </div>
        </details> : null}
        <CopyAction label="复制代码" value={code} compact /><DownloadAction value={code} fileName={downloadName} />
      </div>
    </figcaption>
    {kind ? <div className="paw-rich-code__tabs" role="tablist" aria-label="内容展示方式">
      <button id={`${id}-source`} type="button" role="tab" aria-controls={`${id}-panel`} aria-selected={!showPreview} tabIndex={!showPreview ? 0 : -1} onClick={() => setView('source')}
        onKeyDown={e => { if ((e.key === 'ArrowRight' || e.key === 'ArrowLeft') && !streamingTail) { e.preventDefault(); setView('preview'); document.getElementById(`${id}-preview`)?.focus(); } }}>源码</button>
      <button id={`${id}-preview`} type="button" role="tab" aria-controls={`${id}-panel`} aria-selected={showPreview} tabIndex={showPreview ? 0 : -1} disabled={streamingTail} onClick={() => setView('preview')}
        onKeyDown={e => { if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') { e.preventDefault(); setView('source'); document.getElementById(`${id}-source`)?.focus(); } }}>{kind === 'json' ? '结构' : '预览'}</button>
      {streamingTail ? <small>完整返回后可预览</small> : null}
    </div> : null}
    <div id={`${id}-panel`} role={kind ? 'tabpanel' : undefined} aria-labelledby={kind ? `${id}-${showPreview ? 'preview' : 'source'}` : undefined}>
      {showPreview && kind ? <RichCodePreview key={`${language}:${kind}`} kind={kind} source={code} /> : html ? (
        <div className="agent-code-block__highlight paw-rich-code__scroll" role="region" tabIndex={0} aria-label={`${fileName || language} 代码内容`}
          data-language={language} dangerouslySetInnerHTML={{ __html: html }} />
      ) : <pre className="paw-rich-code__plain paw-rich-code__scroll" data-code-surface role="region" tabIndex={0} aria-label={`${fileName || language} 代码内容`} data-language={language}>
        <code className="agent-code-block__content" data-stream-tail={streamingTail || undefined}>
          {!streamingTail && lines.length <= 2_000 ? lines.map((line, i) => <span className="line" key={i}>{line}{i < lines.length - 1 ? '\n' : ''}</span>) : code}
          <StreamingCursor active={streamingTail} />
        </code>
      </pre>}
    </div>
    {long && !showPreview ? <button className="paw-rich-code__expand" type="button" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
      {expanded ? <Minimize2 size={13} aria-hidden /> : <Maximize2 size={13} aria-hidden />}{expanded ? '收起阅读区' : `展开阅读区 · ${lineCount} 行`}<span>完整内容始终可复制</span>
    </button> : null}
  </figure>;
}

export function StreamingCursor({ active }: { active: boolean }) {
  return active ? <span aria-hidden="true" className="agent-streaming-cursor agent-streaming-cursor--inline" /> : null;
}
