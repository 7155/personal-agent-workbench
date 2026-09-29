import { useId, useState, type ReactNode } from 'react';
import { BookOpen, ChevronDown, FilePenLine, FolderOpen, Globe, Search, SquareTerminal, UsersRound, Wrench, Network, Sparkles, ClipboardList } from 'lucide-react';
import type { ToolCallBlock } from '../model/types';
import { ToolStatusMark } from './ToolStatusMark';
import { toolReceiptPresentation } from '../model/tool-receipt';

const actionLabels: Readonly<Record<string, string>> = {
  workspace_read: '读取文件', workspace_write: '写入文件', workspace_edit: '修改文件',
  workspace_shell: '终端命令', workspace_list: '浏览目录', workspace_search: '搜索文件',
  read: '读取文件', write: '写入文件', edit: '修改文件', bash: '终端命令',
  work_documents: '任务文档', room_partner: '伙伴协作',
};

/** One tool call as a bounded receipt: a reader line always, the raw call and
 *  its evidence one click away. Folding is presentation only — the detail the
 *  host hands over is the same Runtime trace, never a summary of it. */
export function ToolCard({ action, block, detail }: {
  block: ToolCallBlock;
  /** Always-visible host action (a pending approval must never fold away). */
  action?: ReactNode;
  /** Host-rendered body inside the disclosure; supersedes the model output,
   *  which exists so a host without structured evidence still shows one. */
  detail?: ReactNode;
}) {
  const receipt = toolReceiptPresentation(block);
  const [open, setOpen] = useState(block.status === 'error');
  const detailId = useId();
  const hasDetail = Boolean(block.input || block.output || detail);
  const displayName = actionLabels[block.name] ?? block.name;
  const name = block.name.toLowerCase();
  const Glyph = /技能|skill/.test(name) ? Sparkles
    : /jev|路由|调度/.test(name) ? Network
    : /任务文档|work_documents|计划|plan/.test(name) ? ClipboardList
    : /分派|改派|伙伴|协调|room_partner|delegate/.test(name) ? UsersRound
    : /搜索|查找|search|grep|find/.test(name) ? Search
    : /读取|阅读|\bread\b|workspace_read/.test(name) ? BookOpen
    : /写入|修改|write|edit/.test(name) ? FilePenLine
    : /命令|终端|shell|bash|terminal/.test(name) ? SquareTerminal
    : /列出|目录|list|\bls\b/.test(name) ? FolderOpen
    : /浏览|网页|browser|fetch/.test(name) ? Globe : Wrench;
  return (
    <section data-expanded={open && hasDetail} className={`ccui-tool-card status-${receipt.status}`} data-tool-block={block.id} data-execution-outcome={block.executionOutcome}>
      <button
        aria-expanded={hasDetail ? open : undefined}
        aria-controls={hasDetail ? detailId : undefined}
        className="ccui-tool-head"
        disabled={!hasDetail}
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        <Glyph className="ccui-tool-kind" size={17} strokeWidth={1.7} aria-hidden="true" />
        <span className="ccui-tool-main">
          <strong title={displayName !== block.name ? block.name : undefined}>{displayName}</strong>
          {receipt.summary ? <span title={receipt.summary}>{receipt.summary}</span> : null}
        </span>
        <span className="ccui-tool-meta">
          <ToolStatusMark status={receipt.status} size={13} />
          <span className="ccui-tool-state-label" key={receipt.label}>{receipt.label}</span>
          {hasDetail ? <ChevronDown className="ccui-tool-disclosure" size={14} aria-hidden="true" /> : null}
        </span>
      </button>
      {open && hasDetail ? (
        <div className="ccui-tool-body" id={detailId}>
          {block.executionOutcome === 'unknown' && block.summary ? <p>{block.summary}</p> : null}
          {block.input ? <div><span className="ccui-tool-label">调用</span><pre>{block.input}</pre></div> : null}
          {detail ?? (block.output ? <div><span className="ccui-tool-label">证据</span><pre>{block.output}</pre></div> : null)}
        </div>
      ) : null}
      {action ? <footer className="ccui-tool-action">{action}</footer> : null}
    </section>
  );
}
