import { CircleAlert, Clock3, FolderTree, ListChecks, LoaderCircle } from 'lucide-react';
import type { AgentTurnStatus } from '@/contracts/agent-reducer';
import type { Goal } from '@/contracts/generated/agent-workflow-state.v1';

export interface SessionTaskState {
  busy: boolean;
  stopping: boolean;
  paused: boolean;
  pending: boolean;
  waiting: boolean;
  disconnected: boolean;
  error: boolean;
  turnStatus?: AgentTurnStatus;
  goal?: Pick<Goal, 'configured' | 'objective' | 'status'>;
}

/** A reading projection only. Admission, Stop and recovery remain in Session. */
export function sessionTaskStateLabel(state: SessionTaskState): string {
  if (state.disconnected) return '连接恢复中 · 上次状态';
  if (state.stopping) return '正在请求停止';
  if (state.error) return '需要查看问题';
  if (state.waiting) return '等待你处理';
  if (state.paused) return '已暂停 · 进度保留';
  if (state.pending) return '等待响应';
  if (state.busy) return '正在执行';
  if (state.goal?.configured && state.goal.status === 'paused') return '目标已暂停 · 进度保留';
  if (state.goal?.configured && state.goal.status === 'completed') return '任务已完成';
  if (state.goal?.configured && state.goal.status === 'cancelled') return '任务已取消';
  if (state.turnStatus === 'aborted') return '本轮已停止';
  if (state.turnStatus === 'failed') return '本轮未完成';
  if (state.turnStatus === 'completed') return state.goal?.configured && state.goal.status === 'active' ? '本轮已结束 · 任务未完成' : '本轮已结束';
  return state.goal?.configured ? '任务待继续' : '可以继续对话';
}

export function PawSessionTaskbar({ state, title, selected, demo = false, onOpenPanel }: {
  state: SessionTaskState;
  title: string;
  demo?: boolean;
  selected: 'none' | 'files' | 'subagents' | 'status';
  onOpenPanel: (panel: 'files' | 'status', trigger: HTMLButtonElement) => void;
}) {
  const label = sessionTaskStateLabel(state);
  const attention = state.waiting || state.error;
  const Icon = attention ? CircleAlert : state.busy && !state.stopping && !state.paused && !state.disconnected ? LoaderCircle : Clock3;
  return <section className="paw-session-taskbar" aria-label="当前工作" data-attention={attention || undefined}>
    <div className="paw-session-taskbar__copy">
      <span className="paw-session-taskbar__state" role="status" aria-live="polite"><Icon size={15} aria-hidden="true" />{label}{demo ? <small>演示数据</small> : null}</span>
      <strong title={state.goal?.configured ? state.goal.objective : title}>{state.goal?.configured ? state.goal.objective : title}</strong>
    </div>
    <nav aria-label="当前工作内容">
      <button type="button" aria-pressed={selected === 'status'} onClick={event => onOpenPanel('status', event.currentTarget)}><ListChecks size={16} aria-hidden="true" />任务</button>
      <button type="button" aria-pressed={selected === 'files'} onClick={event => onOpenPanel('files', event.currentTarget)}><FolderTree size={16} aria-hidden="true" />文件</button>
    </nav>
  </section>;
}
