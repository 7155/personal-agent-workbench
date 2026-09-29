import { CircleAlert, FolderOpen, ListChecks, Network } from 'lucide-react';
import './paw-workbench-reading.css';

export interface PawSessionFocusHeaderProps {
  title: string;
  busy: boolean;
  stopping: boolean;
  active: boolean;
  hasMessages: boolean;
  needsAttention: boolean;
  readOnly?: boolean;
  panel: string;
  onOpenTasks: () => void;
  onOpenFiles: () => void;
  onOpenSubagents: () => void;
}

/** A view of the existing Session. This header never starts or resumes work. */
export function PawSessionFocusHeader({
  title, busy, stopping, active, hasMessages, needsAttention, readOnly = false,
  panel, onOpenTasks, onOpenFiles, onOpenSubagents,
}: PawSessionFocusHeaderProps) {
  const status = readOnly ? '只读评测记录'
    : stopping ? '正在停止'
    : needsAttention ? '有待处理事项'
    : busy ? '正在执行'
    : hasMessages ? '沿用上下文，继续这段对话' : '说清目标，从这里开始';
  const tone = readOnly ? 'idle' : stopping ? 'stopping' : needsAttention ? 'attention' : busy ? 'running' : 'idle';
  return <header className="paw-session-focus" data-tone={tone} data-motion={active && busy && !stopping && !readOnly ? 'active' : 'paused'}>
    <div className="paw-session-focus__identity">
      <span className="paw-session-focus__eyebrow">{readOnly ? 'EVALUATION' : 'SESSION'}</span>
      <h2 title={title}>{title}</h2>
      <p>{needsAttention && !readOnly ? <CircleAlert size={13} aria-hidden /> : <i aria-hidden />}<span>{status}</span></p>
    </div>
    <nav className="paw-session-focus__actions" aria-label="对话工作区">
      <button type="button" aria-label="打开任务与状态" aria-pressed={panel === 'status'} onClick={onOpenTasks}><ListChecks size={16} aria-hidden /><span>任务</span>{needsAttention && !readOnly ? <i aria-label="有待处理事项" /> : null}</button>
      <button type="button" aria-label="打开对话文件" aria-pressed={panel === 'files'} onClick={onOpenFiles}><FolderOpen size={16} aria-hidden /><span>文件</span></button>
      <button type="button" aria-label="查看子 Agent" aria-pressed={panel === 'subagents'} onClick={onOpenSubagents}><Network size={16} aria-hidden /><span>子 Agent</span></button>
    </nav>
  </header>;
}
