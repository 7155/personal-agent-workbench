import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { BookOpen, Check, ChevronDown, Clipboard, ClipboardList, Code2, FilePenLine, FolderOpen, Globe, Network, Search, Sparkles, SquareTerminal, UsersRound, Wrench } from 'lucide-react';
import { writeClipboardText } from '@/platform/clipboard';
import { usePresentationMotion } from '../reading/reading-preferences';
import type { ToolCallBlock } from '../model/types';
import { codeModeOutputText, codeModeSourceFromInput, isCodeModeTool, type CodeModeCall, type CodeModeDetails } from '../model/codemode';
import { ToolStatusMark } from './ToolStatusMark';
import { toolReceiptPresentation } from '../model/tool-receipt';
import './pi-tool-card.css';

type Panel = 'result' | 'input';
type CopyState = 'idle' | 'pending' | 'done' | 'error';
const actionLabels: Readonly<Record<string, string>> = {
  workspace_read: '读取文件', workspace_write: '写入文件', workspace_edit: '修改文件',
  workspace_shell: '终端命令', workspace_list: '浏览目录', workspace_search: '搜索文件',
  read: '读取文件', write: '写入文件', edit: '修改文件', bash: '终端命令',
  work_documents: '任务文档', room_partner: '伙伴协作',
  codemode: '代码执行',
};
function toolIcon(name: string) {
  const value = name.toLowerCase();
  return /技能|skill/.test(value) ? Sparkles : /jev|路由|调度/.test(value) ? Network
    : /任务文档|work_documents|计划|plan/.test(value) ? ClipboardList
    : /分派|改派|伙伴|协调|room_partner|delegate/.test(value) ? UsersRound
    : /搜索|查找|search|grep|find/.test(value) ? Search
    : /读取|阅读|\bread\b|workspace_read/.test(value) ? BookOpen
    : /写入|修改|write|edit/.test(value) ? FilePenLine
    : /代码执行|codemode/.test(value) ? Code2
    : /命令|终端|shell|bash|terminal/.test(value) ? SquareTerminal
    : /列出|目录|list|\bls\b/.test(value) ? FolderOpen
    : /浏览|网页|browser|fetch/.test(value) ? Globe : Wrench;
}
export function ToolCard({ action, block, detail }: { block: ToolCallBlock; action?: ReactNode; detail?: ReactNode }) {
  const receipt = toolReceiptPresentation(block); const id = useId(); const motion = usePresentationMotion();
  const [open, setOpen] = useState(block.status === 'error'); const [panel, setPanel] = useState<Panel>('result');
  const [copy, setCopy] = useState<{ value: string; state: CopyState }>({ value: '', state: 'idle' });
  const mounted = useRef(true); const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; if (timer.current) clearTimeout(timer.current); }; }, []);
  const currentPanel = panel === 'input' && !block.input ? 'result' : panel;
  const raw = currentPanel === 'input' ? block.input || '' : detail ? '' : block.output || '';
  async function copyVisible() {
    const value = raw; if (!value) return;
    if (timer.current) clearTimeout(timer.current);
    setCopy({ value, state: 'pending' });
    try { await writeClipboardText(value); if (mounted.current) setCopy({ value, state: 'done' }); }
    catch { if (mounted.current) setCopy({ value, state: 'error' }); }
    if (mounted.current) timer.current = setTimeout(() => setCopy({ value: '', state: 'idle' }), 1600);
  }
  return <PiToolReceiptView block={block} action={action} detail={detail} receipt={receipt} detailId={id}
    open={open} panel={currentPanel} motion={motion} copyState={copy.value === raw ? copy.state : 'idle'}
    onToggle={() => setOpen(value => !value)} onPanel={setPanel} onCopy={() => void copyVisible()} />;
}

/** Same render tree in the preview and production host; Runtime status is supplied by its existing owner. */
export function PiToolReceiptView({ block, action, detail, receipt, detailId, open, panel, motion, copyState, onToggle, onPanel, onCopy }: {
  block: ToolCallBlock; action?: ReactNode; detail?: ReactNode; receipt: ReturnType<typeof toolReceiptPresentation>;
  detailId: string; open: boolean; panel: Panel; motion: boolean; copyState: CopyState;
  onToggle: () => void; onPanel: (panel: Panel) => void; onCopy: () => void;
}) {
  const codeMode = isCodeModeTool(block) ? block.codeMode : undefined;
  const codeModeSource = isCodeModeTool(block) ? codeModeSourceFromInput(block.input) : '';
  const codeModeOutput = isCodeModeTool(block) ? codeModeOutputText(block.output) : '';
  const hasDetail = Boolean(block.input || block.output || detail || codeMode); const Glyph = toolIcon(block.name);
  const displayName = actionLabels[block.name] ?? block.name;
  const copyable = panel === 'input' ? Boolean(block.input) : !detail && Boolean(block.output);
  const resultLabel = receipt.status === 'running' && block.output ? '实时片段' : '返回内容';
  const summary = codeMode ? codeModeSummary(codeMode, receipt.status) : receipt.summary;
  return <section data-design="pi-receipt" data-motion={motion} data-expanded={open && hasDetail} data-code-mode={codeMode ? 'true' : undefined} className={`ccui-tool-card status-${receipt.status}`} data-tool-block={block.id} data-execution-outcome={block.executionOutcome}>
    <button type="button" className="ccui-tool-head" aria-expanded={hasDetail ? open : undefined} aria-controls={hasDetail ? detailId : undefined} disabled={!hasDetail} onClick={onToggle}>
      <Glyph className="ccui-tool-kind" size={17} strokeWidth={1.7} aria-hidden />
      <span className="ccui-tool-main"><strong title={displayName !== block.name ? block.name : undefined}>{displayName}</strong>{summary ? <span title={summary}>{summary}</span> : null}</span>
      <span className="ccui-tool-meta"><ToolStatusMark status={receipt.status} size={13} /><span className="ccui-tool-state-label">{receipt.label}</span>{hasDetail ? <ChevronDown className="ccui-tool-disclosure" size={14} aria-hidden /> : null}</span>
    </button>
    {open && hasDetail && codeMode ? <CodeModeBody id={detailId} block={block} details={codeMode} source={codeModeSource} output={codeModeOutput} detail={detail} /> : null}
    {open && hasDetail && !codeMode ? <div className="ccui-tool-body pi-tool-body" id={detailId}>
      {block.executionOutcome === 'unknown' && block.summary ? <p className="pi-tool-uncertain">{block.summary}</p> : null}
      <div className="pi-tool-tabs"><div role="tablist" aria-label={`${displayName}的调用与返回`} onKeyDown={event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) || !block.input) return;
        event.preventDefault(); event.stopPropagation(); const next: Panel = event.key === 'Home' ? 'result' : event.key === 'End' ? 'input' : panel === 'result' ? 'input' : 'result';
        onPanel(next); event.currentTarget.querySelector<HTMLButtonElement>(`[data-panel="${next}"]`)?.focus();
      }}>
        <button type="button" role="tab" data-panel="result" id={`${detailId}-result-tab`} aria-controls={`${detailId}-result`} aria-selected={panel === 'result'} tabIndex={panel === 'result' ? 0 : -1} onClick={() => onPanel('result')}>{resultLabel}</button>
        {block.input ? <button type="button" role="tab" data-panel="input" id={`${detailId}-input-tab`} aria-controls={`${detailId}-input`} aria-selected={panel === 'input'} tabIndex={panel === 'input' ? 0 : -1} onClick={() => onPanel('input')}>调用参数</button> : null}
      </div>{copyable ? <button type="button" className="pi-tool-copy" aria-label={panel === 'input' ? '复制调用参数' : '复制返回片段'} disabled={copyState === 'pending'} onClick={onCopy}>{copyState === 'done' ? <Check size={13} /> : <Clipboard size={13} />}<span role="status">{({ idle: '复制', pending: '复制中', done: '已复制', error: '复制失败' })[copyState]}</span></button> : null}</div>
      <div role="tabpanel" aria-labelledby={`${detailId}-result-tab`} id={`${detailId}-result`} hidden={panel !== 'result'} className="pi-tool-result">
        {detail ?? (block.output ? <pre tabIndex={0} aria-label={`${displayName}返回片段`}>{block.output}</pre> : <p className="pi-tool-empty">{receipt.status === 'running' ? '工具仍在执行，尚无可展示的返回内容。' : '本条回执未附带可展示的返回内容。'}</p>)}
      </div>
      {block.input ? <div role="tabpanel" aria-labelledby={`${detailId}-input-tab`} id={`${detailId}-input`} hidden={panel !== 'input'} className="pi-tool-input"><pre tabIndex={0} aria-label={`${displayName}调用参数`}>{block.input}</pre></div> : null}
    </div> : null}
    {action ? <footer className="ccui-tool-action">{action}</footer> : null}
  </section>;
}

function CodeModeBody({ id, block, details, source, output, detail }: {
  id: string;
  block: ToolCallBlock;
  details: CodeModeDetails;
  source: string;
  output: string;
  detail?: ReactNode;
}) {
  return <div className="ccui-tool-body pi-tool-body pi-code-mode-body" id={id}>
    <section className="pi-code-mode-section" aria-label="代码源码">
      <header><strong>源码</strong><small>JavaScript</small></header>
      <pre tabIndex={0} aria-label="代码执行源码">{source || '本条回执未附带源码。'}</pre>
    </section>
    <section className="pi-code-mode-section" aria-label="嵌套工具调用">
      <header><strong>嵌套工具调用</strong><small>{details.calls.length} 次{details.nestedCallsComplete === false ? ' · 部分恢复' : ''}</small></header>
      {details.calls.length ? <ol className="pi-code-mode-calls">
        {details.calls.map(call => <CodeModeCallRow call={call} key={call.id} />)}
      </ol> : <p className="pi-tool-empty">脚本没有调用其他工具。</p>}
    </section>
    {output ? <section className="pi-code-mode-section" aria-label="代码执行最终输出">
      <header><strong>最终输出</strong></header>
      <pre tabIndex={0} aria-label="代码执行最终输出内容">{output}</pre>
    </section> : null}
    {details.fullOutputPath ? <p className="pi-code-mode-full-output"><span>完整输出</span><code>{details.fullOutputPath}</code></p> : null}
    {detail ? <div className="pi-code-mode-host-detail">{detail}</div> : null}
    {!source && !output && !details.calls.length && !detail && !block.input ? <p className="pi-tool-empty">本条回执未附带可展示的代码执行内容。</p> : null}
  </div>;
}

function CodeModeCallRow({ call }: { call: CodeModeCall }) {
  return <li className="pi-code-mode-call" data-call-status={call.status}>
    <span className="pi-code-mode-call__status" aria-label={codeModeCallStatusLabel(call.status)}>{codeModeCallStatusMark(call.status)}</span>
    <span className="pi-code-mode-call__main"><strong>{call.name}</strong><code>{call.args}</code>{call.error ? <small role="alert">{call.error}</small> : null}<small className="pi-code-mode-call__id">{call.id}</small></span>
    <span className="pi-code-mode-call__meta">{call.durationMs !== undefined ? formatCodeModeDuration(call.durationMs) : null}{call.cost !== undefined ? <>{call.durationMs !== undefined ? ' · ' : ''}{formatCodeModeCost(call.cost)}</> : null}</span>
  </li>;
}

function codeModeSummary(details: CodeModeDetails, status: ReturnType<typeof toolReceiptPresentation>['status']): string {
  const running = details.calls.filter(call => call.status === 'running').length;
  const completeness = details.nestedCallsComplete === false ? ' · 部分恢复' : '';
  if (running) return `${running} 个嵌套调用进行中${completeness}`;
  if (details.calls.length) return `${details.calls.length} 个嵌套调用${completeness} · ${status === 'error' ? '脚本失败' : status === 'cancelled' ? '已停止' : '脚本完成'}`;
  if (details.nestedCallsComplete === false) return `脚本${status === 'error' ? '失败' : status === 'cancelled' ? '已停止' : '完成'} · 嵌套回执部分恢复`;
  return status === 'running' ? '脚本执行中' : status === 'error' ? '脚本失败' : status === 'cancelled' ? '已停止' : '脚本完成';
}

function codeModeCallStatusLabel(status: CodeModeCall['status']): string {
  return ({ running: '进行中', ok: '已完成', error: '失败', cancelled: '已停止' } as const)[status];
}

function codeModeCallStatusMark(status: CodeModeCall['status']): string {
  return ({ running: '…', ok: '✓', error: '×', cancelled: '⊘' } as const)[status];
}

function formatCodeModeDuration(value: number): string {
  return value < 1_000 ? `${Math.round(value)}ms` : `${(value / 1_000).toFixed(1)}s`;
}

function formatCodeModeCost(value: number): string {
  return `$${value >= 0.01 ? value.toFixed(2) : Number(value.toPrecision(2)).toString()}`;
}
