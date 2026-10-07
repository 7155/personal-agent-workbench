import { ArrowUpRight, Check, ChevronDown, FileText, MessageCircle, Pause, Play, Square, TriangleAlert } from 'lucide-react';
import { Fragment, useId, useRef, useState, type ReactNode, type Ref } from 'react';
import type { SessionSummary } from '@/features/agent/types';
import { publicAgentErrorText } from '@/features/agent/public-error';
import { usePresentationMotion } from '@/features/conversation-ui/reading/reading-preferences';
import './controlled-sessions.css';

/** A view of the existing Session owner. This component never loads or runs a Session. */
export type ControlledSessionView = {
  session: SessionSummary;
  task: string;
  statusLabel: string;
  activity: 'running' | 'paused' | 'completed' | 'failed' | 'idle' | 'unknown';
  freshness: 'current' | 'recovering' | 'failed';
  stopTarget?: { turnId: string };
  canContinue?: boolean;
  outputs?: { id: string; title: string; reference: string }[];
};

export function controlledSessionDetail(session: SessionSummary): string {
  const preview = session.lastMessagePreview?.trim();
  if (preview) return preview.slice(0, 160);
  const objective = session.goal?.objective?.trim() ?? '';
  const title = session.title.trim().replace(/(?:…|\.\.\.)$/u, '');
  return objective && !objective.startsWith(title) ? objective : '';
}

export function primaryControlledSessions(owner: SessionSummary, views: ControlledSessionView[]): ControlledSessionView[] {
  const assistantId = owner.metadata?.assistantId;
  if (owner.metadata?.primaryAssistant !== true || !assistantId) return [];
  return views.filter(view => view.session.status !== 'archived' && view.session.metadata?.primaryTask === true
    && view.session.metadata.assistantId === assistantId
    && view.session.metadata.sourceSessionId === owner.id);
}

export function ControlledSessions({ owner, views, name, avatar, onOpen, onStop, onContinue, onOpenOutput, renderSession }: {
  owner: SessionSummary;
  views: ControlledSessionView[];
  name: string;
  avatar?: ReactNode;
  onOpen: (session: SessionSummary) => void;
  /** The calling Session owner must validate the exact target and cancellation receipt. */
  onStop?: (session: SessionSummary, target: { turnId: string }) => Promise<void>;
  /** Only supplied when the Session owner has an available continuation path. */
  onContinue?: (session: SessionSummary) => Promise<void>;
  onOpenOutput?: (session: SessionSummary, output: NonNullable<ControlledSessionView['outputs']>[number]) => void;
  /** Allows an existing live view lease to retain its own subscription policy. */
  renderSession?: (view: ControlledSessionView, index: number, hidden: boolean) => ReactNode;
}) {
  const owned = primaryControlledSessions(owner, views);
  return <ControlledSessionList key={owner.id} {...{ owned, name, avatar, onOpen, onStop, onContinue, onOpenOutput, renderSession }} />;
}

function ControlledSessionList({ owned, name, avatar, onOpen, onStop, onContinue, onOpenOutput, renderSession }: {
  owned: ControlledSessionView[];
  name: string;
  avatar?: ReactNode;
  onOpen: (session: SessionSummary) => void;
  onStop?: (session: SessionSummary, target: { turnId: string }) => Promise<void>;
  onContinue?: (session: SessionSummary) => Promise<void>;
  onOpenOutput?: (session: SessionSummary, output: NonNullable<ControlledSessionView['outputs']>[number]) => void;
  renderSession?: (view: ControlledSessionView, index: number, hidden: boolean) => ReactNode;
}) {
  const [limit, setLimit] = useState(4);
  const [outputLimit, setOutputLimit] = useState(3);
  const sessionsId = useId();
  const outputsId = useId();
  const outputs = owned.flatMap(view => (view.outputs ?? []).map(output => ({ session: view.session, output })));
  const running = owned.filter(view => view.freshness === 'current' && view.activity === 'running').length;
  return <section className="controlled-sessions" aria-label="助手控制的 Sessions">
    <header className="controlled-sessions__identity">
      <span className="controlled-sessions__avatar" aria-hidden="true">{avatar ?? <MessageCircle size={24} />}</span>
      <span><strong>{name}</strong><small>{running ? `${running} 个对话正在进行` : `${owned.length} 个关联任务对话`}</small></span>
    </header>
    <h3>控制的对话 <span>{owned.length}</span></h3>
    <ul id={sessionsId}>{renderSession ? owned.map((view, index) => <Fragment key={view.session.id}>{renderSession(view, index, index >= limit)}</Fragment>) : owned.slice(0, limit).map(view => <ControlledSessionRow key={view.session.id} {...{ view, onOpen, onStop, onContinue }} />)}</ul>
    {!owned.length ? <p className="controlled-sessions__empty">交办任务后，它负责的对话会出现在这里。</p> : null}
    {owned.length > limit ? <button className="controlled-sessions__more" type="button" aria-controls={sessionsId} onClick={() => setLimit(value => value + 4)}><ChevronDown size={14} aria-hidden="true" />显示更多 <span>还有 {owned.length - limit} 个</span></button> : owned.length > 4 ? <button className="controlled-sessions__more" type="button" aria-controls={sessionsId} onClick={() => setLimit(4)}>收起对话</button> : null}
    {outputs.length ? <><h3>产物 <span>{outputs.length}</span></h3><ul id={outputsId}>{outputs.slice(0, outputLimit).map(({ session, output }) => <li key={`${session.id}:${output.id}`} className="controlled-sessions__output">
      <FileText size={16} aria-hidden="true" />
      {onOpenOutput ? <button type="button" title={output.reference} onClick={() => onOpenOutput(session, output)}><strong>{output.title}</strong><small>{session.title}</small></button> : <span title={output.reference}><strong>{output.title}</strong><small>{output.reference}</small></span>}
    </li>)}</ul>{outputs.length > outputLimit ? <button className="controlled-sessions__more" type="button" aria-controls={outputsId} onClick={() => setOutputLimit(value => value + 3)}><ChevronDown size={14} aria-hidden="true" />显示更多产物</button> : outputs.length > 3 ? <button className="controlled-sessions__more" type="button" aria-controls={outputsId} onClick={() => setOutputLimit(3)}>收起产物</button> : null}</> : null}
  </section>;
}

export function ControlledSessionRow({ view, onOpen, onStop, onContinue, rowRef, hidden, disabled, onActivate }: {
  view: ControlledSessionView;
  onOpen: (session: SessionSummary) => void;
  onStop?: (session: SessionSummary, target: { turnId: string }) => Promise<void>;
  onContinue?: (session: SessionSummary) => Promise<void>;
  rowRef?: Ref<HTMLLIElement>;
  hidden?: boolean;
  disabled?: boolean;
  onActivate?: () => void;
}) {
  const [pending, setPending] = useState<'stop' | 'continue'>();
  const [error, setError] = useState('');
  const locked = useRef(false);
  const currentRunning = view.freshness === 'current' && view.activity === 'running';
  const motionAllowed = usePresentationMotion(currentRunning);
  async function act(action: 'stop' | 'continue') {
    if (locked.current || view.freshness !== 'current') return;
    const operation = action === 'stop' && view.stopTarget && onStop ? () => onStop(view.session, view.stopTarget!)
      : action === 'continue' && view.canContinue && onContinue ? () => onContinue(view.session) : undefined;
    if (!operation) return;
    locked.current = true; setPending(action); setError('');
    try { await operation(); }
    catch (reason) { setError(publicAgentErrorText(reason, '操作尚未确认，请打开对话查看。')); }
    finally { locked.current = false; setPending(undefined); }
  }
  const Icon = view.activity === 'completed' ? Check : view.activity === 'paused' ? Pause : view.activity === 'failed' ? TriangleAlert : MessageCircle;
  const stop = Boolean(onStop && view.stopTarget && view.freshness === 'current');
  const resume = Boolean(onContinue && view.canContinue && !view.stopTarget && view.freshness === 'current');
  const status = view.freshness === 'current' ? view.statusLabel : `${view.freshness === 'failed' ? '暂时无法同步' : '正在重新同步'} · 上次状态：${view.statusLabel}`;
  return <li className="controlled-sessions__row" data-activity={view.activity} data-control-active={currentRunning || undefined} data-motion={motionAllowed ? 'active' : 'paused'} ref={rowRef} hidden={hidden}>
    <span className="controlled-sessions__connection" aria-hidden="true"><i /></span>
    <Icon className="controlled-sessions__mark" size={16} aria-hidden="true" />
    <button className="controlled-sessions__open" type="button" disabled={disabled} aria-label={`打开 ${view.session.title}${view.task && view.task !== view.session.title ? ` · ${view.task}` : ''} · ${status}`} onFocus={onActivate} onPointerEnter={onActivate} onClick={() => onOpen(view.session)}>
      <strong>{view.session.title}</strong>{view.task && view.task !== view.session.title ? <span>{view.task}</span> : null}<small>{status}</small>
    </button>
    <div className="controlled-sessions__actions">
      {stop ? <button type="button" aria-label={`停止 ${view.session.title}`} aria-busy={pending === 'stop'} disabled={Boolean(pending)} onClick={() => void act('stop')}><Square size={13} aria-hidden="true" /><span>停止</span></button> : null}
      {resume ? <button type="button" aria-label={`继续 ${view.session.title}`} aria-busy={pending === 'continue'} disabled={Boolean(pending)} onClick={() => void act('continue')}><Play size={13} aria-hidden="true" /><span>继续</span></button> : null}
      {!stop && !resume ? <ArrowUpRight size={14} aria-hidden="true" /> : null}
    </div>
    {error ? <p className="controlled-sessions__error" role="alert">{error}</p> : null}
  </li>;
}
