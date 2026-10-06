import { ArrowUpRight, BookOpen, Check, ChevronRight, Folder, LoaderCircle, MessageCircle, Send } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { textCodePointCount, trimContractText } from '@/contracts/text-budget';
import { useProductIdentity } from '@/features/identity/product-identity';
import { sessionItems, type SessionSummary } from '@/features/agent/types';
import { publicAgentErrorText } from '@/features/agent/public-error';
import { publicErrorText } from '@/features/overview/management-ui';
import { useAgentLiveSession, type AgentLiveSnapshotLoader, type AgentRecoveryState } from '@/features/agent/runtime/use-agent-live-session';
import { agentSessionAddress, latestActiveAgentTurnId, selectAgentProjection, useAgentLiveStore } from '@/features/agent/state/live-store';
import { usePageVisibility } from '@/platform/use-page-visibility';
import { openPawOsRoute, usePawOsDesktop } from '@/features/paw-os/surface-context';
import { warmAgentWorkspace, type InitialSessionSubmission, type PrimaryAssistantSource } from './agent-workspace-loader';
import '@/features/composer/composer-workbench.css';
import './primary-assistant.css';

export type PrimaryAssistantHomeDraft = { draft: string; execute: boolean; workspace: string; contextWorkspace: string;
  acceptance: string; scopeConfirmed: boolean; source?: PrimaryAssistantSource; attempt?: { signature: string; id: string } };

/** A small entry into ordinary Sessions. Pi still owns every turn and Stop. */
export function PawPrimaryAssistantHome({ initialDraft = '', initialExecute = false, initialSource, initialForm, projectRoots = [], onOpen, onAdvanced, onRememberDraft }: {
  initialDraft?: string; initialExecute?: boolean; initialSource?: PrimaryAssistantSource; projectRoots?: string[];
  initialForm?: PrimaryAssistantHomeDraft; onRememberDraft?: (draft?: PrimaryAssistantHomeDraft) => void;
  onOpen: (session: SessionSummary, submission?: InitialSessionSubmission, draft?: string) => void;
  onAdvanced: (draft: string) => void;
}) {
  const transport = useControlTransport();
  const desktop = usePawOsDesktop();
  const identity = useProductIdentity();
  const pageVisible = usePageVisibility();
  const [session, setSession] = useState<SessionSummary>();
  const [tasks, setTasks] = useState<SessionSummary[]>([]);
  const [allTasks, setAllTasks] = useState(false);
  const [draft, setDraft] = useState(initialForm?.draft ?? initialDraft);
  const [intent, setIntent] = useState<'discuss' | 'execute'>((initialForm?.execute ?? initialExecute) ? 'execute' : 'discuss');
  const [workspace, setWorkspace] = useState(initialForm?.workspace ?? initialSource?.workspaceRoots[0] ?? '');
  const [contextWorkspace, setContextWorkspace] = useState(initialForm?.contextWorkspace ?? initialSource?.workspaceRoots[0] ?? '');
  const [acceptance, setAcceptance] = useState(initialForm?.acceptance ?? '');
  const [scopeConfirmed, setScopeConfirmed] = useState(initialForm?.scopeConfirmed ?? false);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [pickingWorkspace, setPickingWorkspace] = useState(false);
  const [pickerNotice, setPickerNotice] = useState('');
  const picker = useRef<object | undefined>(undefined);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const lock = useRef(false);
  const owner = useRef(0);
  const readSequence = useRef(0);
  const attempt = useRef<{ signature: string; id: string } | undefined>(initialForm?.attempt);
  const attemptTransport = useRef(transport);
  const sourceTransport = useRef(transport);
  const refreshSource = useRef(initialSource);
  const input = useRef<HTMLTextAreaElement>(null);
  const workspaceInput = useRef<HTMLInputElement>(null);
  const fields = useRef<HTMLDivElement>(null);
  const acceptanceInput = useRef<HTMLTextAreaElement>(null);
  const composingRef = useRef(false);
  const draftConsumed = useRef(false);
  const rememberLatestDraft = useRef<() => void>(() => undefined);
  const composerHintId = useId();
  const objectiveErrorId = useId();
  const acceptanceErrorId = useId();
  const workspaceErrorId = useId();
  const message = trimContractText(draft);
  const criteria = acceptance.split('\n').map(trimContractText).filter(Boolean);
  const objectiveLength = textCodePointCount(message);
  const criteriaLength = textCodePointCount(criteria.join('\n'));
  const objectiveInvalid = intent === 'execute' && objectiveLength > 4000;
  const criteriaInvalid = intent === 'execute' && (criteria.length > 20 || criteriaLength > 2000);
  const executionRoots = workspace.trim() === initialSource?.workspaceRoots[0]
    ? initialSource.workspaceRoots : workspace.trim() ? [workspace.trim()] : [];
  const sourceRoots = session?.workspaceRoots ?? [];
  const projectMismatch = intent === 'execute' && sourceRoots.length > 0 && JSON.stringify(sourceRoots) !== JSON.stringify(executionRoots);
  const taskInvalid = objectiveInvalid || criteriaInvalid || projectMismatch;
  const baseSubmitHint = submitting ? (intent === 'execute' ? '正在确认任务，请稍候…' : '正在打开对话…')
    : pickingWorkspace ? (intent === 'execute' ? '正在选择目录，选好后再确认授权。' : '正在选择项目，选好后继续讨论。')
    : loading ? '正在连接对话，可以先写下想法。'
    : !session ? '暂时无法发送。草稿保留在这里，请重新连接。'
    : projectMismatch ? '执行目录需要与当前讨论项目一致。草稿会完整保留。'
    : taskInvalid ? '请先修改超出限制的任务内容。草稿会完整保留。'
    : intent === 'execute' && !workspace.trim() ? '先选择本次工作目录。'
    : intent === 'execute' && !scopeConfirmed ? '确认目录权限后，才会开始执行。'
    : 'Enter 发送 · Shift + Enter 换行';
  const submitHint = pickerNotice ? `${pickerNotice} ${baseSubmitHint}` : baseSubmitHint;
  useEffect(() => {
    setSubmitting(false); setSession(undefined); setTasks([]); lock.current = false;
    picker.current = undefined; setPickingWorkspace(false); setPickerNotice('');
    if (attemptTransport.current !== transport) { attempt.current = undefined; attemptTransport.current = transport; setScopeConfirmed(false); }
    return () => { owner.current += 1; };
  }, [transport]);
  useEffect(() => {
    const generation = ++readSequence.current;
    const controller = new AbortController();
    // Keep the last confirmed view during a same-context refresh. Admission
    // remains gated by loading; project/transport changes clear identity first.
    setLoading(true); setError('');
    if (refreshSource.current !== initialSource) { setSession(undefined); setTasks([]); setAllTasks(false); refreshSource.current = initialSource; }
    const workspaceRoots = contextWorkspace === initialSource?.workspaceRoots[0]
      ? initialSource.workspaceRoots : contextWorkspace ? [contextWorkspace] : [];
    void transport.request<Record<string, unknown>>({ pathId: 'agent.primary.ensure', body: { workspaceRoots }, signal: controller.signal }).then(result => {
      if (readSequence.current !== generation || controller.signal.aborted) return;
      const primary = sessionItems({ items: [result.session] }, { includeAppOwned: true })[0];
      if (!primary?.metadata?.primaryAssistant) throw new Error('长期助手的会话身份尚未确认。');
      setSession(primary);
      setTasks(sessionItems({ items: result.tasks }, { includeAppOwned: true }));
    }).catch(reason => {
      if (readSequence.current === generation && !controller.signal.aborted) { setSession(undefined); setError(publicErrorText(reason, '暂时无法读取已有对话，请重新连接。')); }
    }).finally(() => { if (readSequence.current === generation && !controller.signal.aborted) setLoading(false); });
    return () => { controller.abort(); readSequence.current += 1; };
  }, [transport, revision, contextWorkspace, initialSource]);
  useEffect(() => {
    const refresh = () => { if (document.visibilityState !== 'hidden') setRevision(value => value + 1); };
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', refresh);
    return () => { window.removeEventListener('focus', refresh); document.removeEventListener('visibilitychange', refresh); };
  }, []);

  function rememberDraft() {
    onRememberDraft?.({ draft, execute: intent === 'execute', workspace, contextWorkspace, acceptance, scopeConfirmed, source: initialSource, attempt: attempt.current });
  }
  function openSession(target: SessionSummary, submission?: InitialSessionSubmission, text?: string) {
    if (submission) { draftConsumed.current = true; onRememberDraft?.(); } else rememberDraft();
    if (text !== undefined) onOpen(target, submission, text);
    else if (submission) onOpen(target, submission);
    else onOpen(target);
  }

  async function submit() {
    if (lock.current || picker.current || !session || !message || loading) return;
    if (taskInvalid) { (objectiveInvalid ? input : criteriaInvalid ? acceptanceInput : workspaceInput).current?.focus(); return; }
    if (intent === 'execute' && (!workspace.trim() || !scopeConfirmed)) {
      setError('请指定本次工作的目录，并确认这个目录内的执行权限。'); return;
    }
    lock.current = true; setSubmitting(true); setError('');
    const generation = owner.current;
    const sourceMessageId = sourceTransport.current === transport && initialSource?.sessionId === session.id ? initialSource.messageId : undefined;
    const signature = JSON.stringify([session.id, sourceMessageId, intent, message, executionRoots, acceptance.trim()]);
    if (attempt.current?.signature !== signature) attempt.current = { signature, id: `primary-${crypto.randomUUID()}` };
    const clientMessageId = attempt.current.id;
    try {
      let target = session;
      if (intent === 'execute') {
        const result = await transport.request<Record<string, unknown>>({ pathId: 'agent.primary.tasks.create', body: {
          clientRequestId: clientMessageId, sourceSessionId: session.id, objective: message,
          ...(sourceMessageId ? { sourceMessageId } : {}),
          acceptanceCriteria: criteria,
          workspaceRoots: executionRoots, workspaceScopeConfirmation: 'APPROVE_WORKSPACE_SCOPE',
        } });
        if (generation !== owner.current) return;
        const created = sessionItems({ items: [result.session] }, { includeAppOwned: true })[0];
        if (!created?.metadata?.primaryTask) throw new Error('任务会话尚未确认。草稿已保留，请重新读取。');
        target = created;
      }
      if (generation !== owner.current) return;
      // No prompt endpoint here: the existing workspace admits this identity
      // exactly once and owns optimistic rows, reconnection, results and Stop.
      openSession(target, { clientMessageId, message });
    } catch (reason) {
      if (generation === owner.current) setError(`${publicErrorText(reason, '本次任务尚未确认，请重试。')} 草稿已保留；重试会核对同一次请求。`);
    } finally {
      if (generation === owner.current) { lock.current = false; setSubmitting(false); }
    }
  }

  async function chooseWorkspace(forDiscussion = false) {
    if (!transport.pickFiles || picker.current || lock.current) return;
    const generation = owner.current;
    const request = {};
    picker.current = request; setPickingWorkspace(true); setPickerNotice(''); setError('');
    const isCurrent = () => generation === owner.current && picker.current === request;
    try {
      const files = await transport.pickFiles({ purpose: 'workspace-root', selection: 'directory', multiple: false, maxFiles: 1 });
      if (!isCurrent()) return;
      const path = files[0]?.path?.trim();
      if (path) {
        if (forDiscussion) selectDiscussionProject(path);
        else { setWorkspace(path); setScopeConfirmed(false); }
      } else setPickerNotice('已取消选择，原目录保持不变。');
    } catch (reason) { if (isCurrent()) setError(publicErrorText(reason, '未能打开目录选择器，请重试。')); }
    finally { if (isCurrent()) { picker.current = undefined; setPickingWorkspace(false); } }
  }

  function selectDiscussionProject(path: string) {
    // Retire admission synchronously with the user's selection. Do not leave
    // one render where the new project can submit into the old discussion.
    if (path !== contextWorkspace) { setLoading(true); setSession(undefined); setTasks([]); setAllTasks(false); setContextWorkspace(path); }
    setWorkspace(path); setScopeConfirmed(false); setPickerNotice('');
  }

  // A focused task field must remain editable when its available scroll area
  // shrinks (window resize or a keyboard). Keep this within the existing form.
  useEffect(() => {
    const element = fields.current;
    if (!element || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => {
      const focused = document.activeElement;
      if (!(focused instanceof HTMLElement) || !element.contains(focused)) return;
      const bounds = focused.getBoundingClientRect();
      const visible = element.getBoundingClientRect();
      if (bounds.bottom > visible.bottom) element.scrollTop += bounds.bottom - visible.bottom;
      else if (bounds.top < visible.top) element.scrollTop -= visible.top - bounds.top;
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  // Parent-owned navigation (history, deep links, window close) can leave Home
  // without calling one of its own buttons. Keep the latest unsubmitted form.
  rememberLatestDraft.current = rememberDraft;
  useEffect(() => () => {
    if (!draftConsumed.current) rememberLatestDraft.current();
  }, []);

  return <div className="paw-primary-home" data-intent={intent}>
    <div className="paw-primary-home__body">
      <div className="paw-primary-home__entry">
      <header className="paw-primary-home__heading">
        <div><h1>继续之前的事。</h1><p>和 {identity.assistantName} 继续同一段对话。</p></div>
        {session ? <button className="paw-primary-home__continue" disabled={submitting || pickingWorkspace || loading} onClick={() => openSession(session, undefined, draft)} onPointerEnter={() => warmAgentWorkspace('session')} title="进入这段对话；草稿会带入输入框，点击发送后才会提交" type="button">进入对话 <ArrowUpRight size={15} /></button> : null}
      </header>
      <section className="paw-primary-home__composer agent-composer paw-unified-composer" data-composer-design="workbench" aria-label="我的长期助手">
        <div className="paw-primary-home__fields" ref={fields}>
        <textarea aria-label="和我的助手聊聊" aria-invalid={objectiveInvalid || undefined} aria-describedby={`${composerHintId}${objectiveInvalid ? ` ${objectiveErrorId}` : ''}`} ref={input} value={draft} disabled={submitting} onChange={event => setDraft(event.target.value)} onCompositionStart={() => { composingRef.current = true; }} onCompositionEnd={() => { composingRef.current = false; }} onKeyDown={event => {
          if (composingRef.current || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
          if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void submit(); }
        }} placeholder={intent === 'discuss' ? '写下想法，或接着做的事…' : '这次要完成什么？'} rows={3} />
        {intent === 'execute' ? <small className="paw-primary-home__field-counter">{objectiveLength} / 4000 字</small> : null}
        {objectiveInvalid ? <p className="paw-primary-home__field-error" id={objectiveErrorId} role="alert">任务目标最多 4000 字，请精简后再授权。</p> : null}
        {intent === 'execute' ? <div className="paw-primary-home__scope">
          <p className="paw-primary-home__scope-note">会参考这段讨论中近期的消息，不包含全部历史。请确认要做什么、怎样算完成，以及允许操作的目录。</p>
          <label>完成标准 <span className="paw-primary-home__criteria-help"><span>可选 · 每行一项</span><span>{criteria.length} / 20 项 · {criteriaLength} / 2000 字（含换行）</span></span><textarea aria-label="完成标准" aria-invalid={criteriaInvalid || undefined} aria-describedby={criteriaInvalid ? acceptanceErrorId : undefined} ref={acceptanceInput} value={acceptance} disabled={submitting} onChange={event => setAcceptance(event.target.value)} placeholder="例如：测试通过，并说明修改了什么" rows={2} /></label>
          {criteriaInvalid ? <p className="paw-primary-home__field-error" id={acceptanceErrorId} role="alert">完成标准最多 20 项，总计最多 2000 字（含换行）。请精简后再授权。</p> : null}
          <label>本次工作目录<div className="paw-primary-home__folder"><Folder aria-hidden="true" size={15} /><input aria-label="本次工作目录" ref={workspaceInput} aria-invalid={projectMismatch || undefined} aria-describedby={projectMismatch ? workspaceErrorId : undefined} value={workspace} disabled={submitting || pickingWorkspace} onChange={event => { setWorkspace(event.target.value); setScopeConfirmed(false); setPickerNotice(''); }} placeholder="/path/to/project" />{transport.pickFiles ? <button disabled={submitting || pickingWorkspace} aria-busy={pickingWorkspace} onClick={() => void chooseWorkspace()} type="button">{pickingWorkspace ? '正在选择…' : '选择目录'}</button> : null}</div></label>
          {projectMismatch ? <p className="paw-primary-home__field-error" id={workspaceErrorId} role="alert">执行目录与当前讨论项目不一致。<span className="paw-primary-home__source-roots">讨论项目：{sourceRoots.map(root => <code key={root}>{root}</code>)}</span>要换项目，请回到“聊一聊”选择讨论项目。</p> : null}
          {executionRoots.length > 1 ? <ul className="paw-primary-home__root-list" aria-label="本次授权目录">{executionRoots.map(root => <li key={root}>{root}</li>)}</ul> : null}
        </div> : null}
        </div>
        {intent === 'execute' ? <label className="paw-primary-home__consent"><input type="checkbox" checked={scopeConfirmed} disabled={submitting || pickingWorkspace || !workspace.trim()} onChange={event => setScopeConfirmed(event.target.checked)} /><span>允许助手在{executionRoots.length > 1 ? `以上 ${executionRoots.length} 个目录` : '这个目录'}内执行本次任务、修改文件和运行命令。可以随时停止。</span></label> : null}
        <footer className="agent-composer__toolbar"><div className="paw-primary-home__intent agent-composer__controls" role="group" aria-label="本次意图"><button aria-pressed={intent === 'discuss'} disabled={submitting || pickingWorkspace} onClick={() => { setIntent('discuss'); setError(''); input.current?.focus(); }} type="button"><MessageCircle size={14} />聊一聊</button><button aria-pressed={intent === 'execute'} disabled={submitting || pickingWorkspace} onClick={() => { setIntent('execute'); setError(''); input.current?.focus(); }} type="button"><Check size={14} />交给助手做</button></div><button aria-label={intent === 'discuss' ? '发送给我的助手' : '授权并开始任务'} aria-describedby={composerHintId} aria-busy={submitting} className={`paw-primary-home__send${intent === 'discuss' ? ' agent-composer__send' : ''}`} disabled={loading || submitting || pickingWorkspace || !session || !message || taskInvalid || (intent === 'execute' && (!workspace.trim() || !scopeConfirmed))} onClick={() => void submit()} type="button">{submitting ? <LoaderCircle className="ui-spin" size={17} /> : intent === 'discuss' ? <Send size={16} /> : <Check size={17} />}<span>{intent === 'discuss' ? null : '授权并开始'}</span></button></footer>
        <p data-essential={baseSubmitHint !== 'Enter 发送 · Shift + Enter 换行' || Boolean(pickerNotice) || undefined} className="paw-primary-home__composer-hint" id={composerHintId} role="status">{submitHint}</p>
      </section>
      </div>
      <div className="paw-primary-home__support">{intent === 'discuss' ? <div className="paw-primary-home__project"><Folder aria-hidden="true" size={13} /><select aria-label="讨论项目" disabled={submitting || pickingWorkspace || loading} value={contextWorkspace} onChange={event => selectDiscussionProject(event.target.value)}><option value="">日常对话 · 不绑定项目</option>{Array.from(new Set([...projectRoots, ...(contextWorkspace ? [contextWorkspace] : [])])).map(root => <option key={root} value={root}>{root}</option>)}</select>{transport.pickFiles ? <button disabled={submitting || pickingWorkspace || loading} aria-busy={pickingWorkspace} onClick={() => void chooseWorkspace(true)} type="button">{pickingWorkspace ? '正在选择…' : '选择项目'}</button> : null}</div> : null}
      <div className="paw-primary-home__hint"><span>{intent === 'discuss' ? '讨论 · 只读' : '本次任务 · 授权目录内执行'}</span>{desktop ? <button onClick={() => openPawOsRoute(desktop, '/memory?view=profile')} type="button"><BookOpen size={13} />关于我</button> : null}</div>
      </div>
      {transport.kind === 'mock' ? <p className="paw-primary-home__demo">演示模式 · 合成数据，不调用真实模型</p> : null}
      {error ? <div className="paw-primary-home__error" role="alert">{error}{!session && !loading ? <button onClick={() => setRevision(value => value + 1)} type="button">重新连接</button> : null}</div> : null}
      <section className="paw-primary-home__tasks" data-empty={!tasks.length || undefined} aria-label="助手的任务"><header><h2>接着做</h2><span>{tasks.length ? `${tasks.length} 个任务` : '一件事，一段清楚的记录'}</span></header>{tasks.length ? <ul>{tasks.map((task, index) => <PrimaryTaskRow key={task.id} task={task} initiallyVisible={index < 4} hidden={!allTasks && index >= 4} pageVisible={pageVisible} disabled={submitting || pickingWorkspace} onOpen={target => openSession(target)} />)}</ul> : <p>{loading ? '正在读取任务记录…' : !session ? '连接恢复后会显示任务记录。' : '交给助手的工作会出现在这里，过程、结果和停止入口都在任务里。'}</p>}{tasks.length > 4 ? <button className="paw-primary-home__advanced" onClick={() => setAllTasks(value => !value)} type="button">{allTasks ? '收起任务' : `查看全部 ${tasks.length} 个任务`}</button> : null}</section>
      <button className="paw-primary-home__advanced" onClick={() => { rememberDraft(); onAdvanced(draft); }} disabled={submitting || pickingWorkspace} type="button">新建独立对话或多人协作 <ChevronRight size={13} /></button>
    </div>
  </div>;
}

/** A view lease, not a second stream/recovery owner. Hidden idle rows stay cold. */
function PrimaryTaskRow({ task, hidden, initiallyVisible, pageVisible, disabled, onOpen }: {
  task: SessionSummary; hidden: boolean; initiallyVisible: boolean; pageVisible: boolean; disabled: boolean;
  onOpen: (session: SessionSummary) => void;
}) {
  const transport = useControlTransport();
  const row = useRef<HTMLLIElement>(null);
  const [visible, setVisible] = useState(initiallyVisible);
  const scopeRef = useRef({ transport, id: task.id });
  if (scopeRef.current.transport !== transport || scopeRef.current.id !== task.id) scopeRef.current = { transport, id: task.id };
  const scope = scopeRef.current;
  const [acceptedScope, setAcceptedScope] = useState<typeof scope>();
  const [recovery, setRecovery] = useState<{ scope: typeof scope; state: AgentRecoveryState }>();
  const address = agentSessionAddress(transport, task.id);
  const projection = useAgentLiveStore(state => selectAgentProjection(state, address));
  const current = acceptedScope === scope ? projection : undefined;
  const busy = current ? Boolean(current.durableRecovery?.compactionTarget
    || current.durableRecovery?.activeTurn?.turnId || latestActiveAgentTurnId(current)
    || ['busy', 'analyzing', 'working', 'waiting', 'retrying', 'aborting', 'stopping'].includes(current.status)) : task.status === 'busy';
  useEffect(() => {
    if (hidden) { setVisible(false); return; }
    if (typeof IntersectionObserver === 'undefined') { setVisible(initiallyVisible); return; }
    const observer = new IntersectionObserver(entries => setVisible(entries.some(entry => entry.isIntersecting)));
    if (row.current) observer.observe(row.current);
    return () => observer.disconnect();
  }, [hidden, initiallyVisible]);
  const loadRef = useRef<AgentLiveSnapshotLoader>(async () => false);
  const load = useAgentLiveSession({
    sessionId: task.id, transport, active: pageVisible && ((!hidden && visible) || busy), snapshotView: 'recent',
    onSnapshot: () => setAcceptedScope(scope),
    onRecoveryState: state => setRecovery({ scope, state }),
    onEvent: event => {
      if (['turn_completed', 'turn_failed', 'compaction_completed'].includes(event.eventType)) {
        void loadRef.current({ preserveAfterSequence: event.sequence });
      }
    },
  });
  loadRef.current = load;
  const liveGoal = current?.goal;
  const useGoal = liveGoal?.sessionId === task.id && (liveGoal.revision > 0 || Boolean(liveGoal.goalId))
    && (!task.goal || liveGoal.goalId !== task.goal.goalId || liveGoal.revision >= task.goal.revision);
  const latestAnswer = current && [...current.messageOrder].reverse().map(id => current.messagesById[id])
    .find(message => message?.role === 'assistant' && message.status === 'completed');
  const preview = latestAnswer?.blocks.filter(block => block.type === 'text')
    .map(block => typeof block.data.text === 'string' ? block.data.text : '').join('\n').trim().slice(0, 180);
  const displayed: SessionSummary = current ? {
    ...task,
    status: task.status === 'archived' ? 'archived' : busy ? 'busy' : ['failed', 'faulted'].includes(current.status) ? 'faulted' : 'idle',
    ...(useGoal ? { goal: liveGoal } : {}),
    ...(preview ? { lastMessagePreview: preview } : {}),
  } : task;
  const status = current?.durableRecovery?.paused && !current.durableRecovery.compactionTarget ? '已暂停' : taskStatus(displayed);
  const recoveryState = recovery?.scope === scope ? recovery.state : undefined;
  const progress = recoveryState === 'failed' ? `暂时无法同步 · 上次状态：${status}`
    : recoveryState === 'recovering' ? `正在重新同步 · 上次状态：${status}` : status;
  return <li ref={row} hidden={hidden}><button disabled={disabled} onClick={() => onOpen(displayed)} onFocus={() => setVisible(true)} onPointerEnter={() => { setVisible(true); warmAgentWorkspace('session'); }} type="button"><span><strong>{task.title}</strong><small>{progress}{displayed.lastMessagePreview ? ` · ${displayed.lastMessagePreview}` : ''}</small></span><ChevronRight size={16} /></button></li>;
}

function taskStatus(task: SessionSummary): string {
  if (task.status === 'busy') return '进行中';
  if (task.status === 'faulted') return '需要查看';
  if (task.status === 'archived') return '已归档';
  const goalStatus = task.goal?.status;
  if (goalStatus === 'completed') return '已完成';
  if (goalStatus === 'paused') return '已暂停';
  if (goalStatus === 'cancelled') return '已取消';
  return goalStatus === 'active' ? '任务未完成' : '查看进度';
}
