import { ArrowUp, ArrowUpRight, BookOpen, Check, ChevronRight, Folder, LoaderCircle, MessageCircle, Sparkles } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { useProductIdentity } from '@/features/identity/product-identity';
import { sessionItems, type SessionSummary } from '@/features/agent/types';
import { publicAgentErrorText } from '@/features/agent/public-error';
import { openPawOsRoute, usePawOsDesktop } from '@/features/paw-os/surface-context';
import { warmAgentWorkspace, type InitialSessionSubmission, type PrimaryAssistantSource } from './agent-workspace-loader';
import './primary-assistant.css';

/** A small entry into ordinary Sessions. Pi still owns every turn and Stop. */
export function PawPrimaryAssistantHome({ initialDraft = '', initialExecute = false, initialSource, projectRoots = [], onOpen, onAdvanced }: {
  initialDraft?: string; initialExecute?: boolean; initialSource?: PrimaryAssistantSource; projectRoots?: string[];
  onOpen: (session: SessionSummary, submission?: InitialSessionSubmission) => void;
  onAdvanced: () => void;
}) {
  const transport = useControlTransport();
  const desktop = usePawOsDesktop();
  const identity = useProductIdentity();
  const [session, setSession] = useState<SessionSummary>();
  const [tasks, setTasks] = useState<SessionSummary[]>([]);
  const [allTasks, setAllTasks] = useState(false);
  const [draft, setDraft] = useState(initialDraft);
  const [intent, setIntent] = useState<'discuss' | 'execute'>(initialExecute ? 'execute' : 'discuss');
  const [workspace, setWorkspace] = useState(initialSource?.workspaceRoots[0] ?? '');
  const [contextWorkspace, setContextWorkspace] = useState(initialSource?.workspaceRoots[0] ?? '');
  const [acceptance, setAcceptance] = useState('');
  const [scopeConfirmed, setScopeConfirmed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const lock = useRef(false);
  const owner = useRef(0);
  const readSequence = useRef(0);
  const attempt = useRef<{ signature: string; id: string } | undefined>(undefined);
  const input = useRef<HTMLTextAreaElement>(null);
  const executionRoots = workspace.trim() === initialSource?.workspaceRoots[0]
    ? initialSource.workspaceRoots : workspace.trim() ? [workspace.trim()] : [];
  const taskCursors = useRef({ transport, values: new Map<string, string>() });
  if (taskCursors.current.transport !== transport) taskCursors.current = { transport, values: new Map() };
  useEffect(() => {
    setSubmitting(false); lock.current = false; attempt.current = undefined;
    return () => { owner.current += 1; };
  }, [transport]);
  useEffect(() => {
    const generation = ++readSequence.current;
    const controller = new AbortController();
    setLoading(true); setError(''); setSession(undefined);
    const workspaceRoots = contextWorkspace === initialSource?.workspaceRoots[0]
      ? initialSource.workspaceRoots : contextWorkspace ? [contextWorkspace] : [];
    void transport.request<Record<string, unknown>>({ pathId: 'agent.primary.ensure', body: { workspaceRoots }, signal: controller.signal }).then(result => {
      if (readSequence.current !== generation || controller.signal.aborted) return;
      const primary = sessionItems({ items: [result.session] }, { includeAppOwned: true })[0];
      if (!primary?.metadata?.primaryAssistant) throw new Error('长期助手的会话身份尚未确认。');
      setSession(primary);
      setTasks(sessionItems({ items: result.tasks }, { includeAppOwned: true }));
    }).catch(reason => {
      if (readSequence.current === generation && !controller.signal.aborted) setError(publicAgentErrorText(reason));
    }).finally(() => { if (readSequence.current === generation && !controller.signal.aborted) setLoading(false); });
    return () => { controller.abort(); readSequence.current += 1; };
  }, [transport, revision, contextWorkspace, initialSource]);
  const liveTaskIds = tasks.filter((task, index) => task.status === 'busy' || (index < (allTasks ? tasks.length : 4) && task.goal?.status === 'active')).map(task => task.id).sort().join('\n');
  useEffect(() => {
    const refresh = () => { if (document.visibilityState !== 'hidden') setRevision(value => value + 1); };
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', refresh);
    return () => { window.removeEventListener('focus', refresh); document.removeEventListener('visibilitychange', refresh); };
  }, []);
  useEffect(() => {
    if (!liveTaskIds) return;
    let active = true;
    let queued = false;
    const refresh = () => {
      if (!active || queued) return;
      queued = true;
      queueMicrotask(() => { queued = false; if (active) setRevision(value => value + 1); });
    };
    const unsubscribers = liveTaskIds.split('\n').map(sessionId => transport.subscribe({ pathId: 'agent.session.events', params: { sessionId }, lastEventId: taskCursors.current.values.get(sessionId) ?? '' }, {
      next: value => {
        const event = value as { eventType?: string; sessionId?: string; eventId?: string; resumeToken?: string };
        if (event.sessionId !== sessionId) return;
        const cursor = event.resumeToken || event.eventId;
        if (cursor && cursor === taskCursors.current.values.get(sessionId)) return;
        if (cursor) taskCursors.current.values.set(sessionId, cursor);
        if (event.sessionId === sessionId && ['turn_completed', 'turn_failed', 'workflow_changed'].includes(event.eventType ?? '')) refresh();
      },
      snapshotRequired: refresh,
    }));
    return () => { active = false; unsubscribers.forEach(unsubscribe => unsubscribe()); };
  }, [transport, liveTaskIds]);

  async function submit() {
    const message = draft.trim();
    if (lock.current || !session || !message || loading) return;
    if (intent === 'execute' && (!workspace.trim() || !scopeConfirmed)) {
      setError('请指定本次工作的目录，并确认这个目录内的执行权限。'); return;
    }
    if (intent === 'execute' && /^(?:就)?(?:按(?:照)?|照)(?:刚才|之前|上面|前面)(?:说的|讨论的|的计划|的方案)?(?:做|执行)?[。！!\s]*$|^(?:开始吧|就这样做|按计划做|照着做)[。！!\s]*$/u.test(message)) {
      setError('请把要做的事和必要背景写进目标。本次任务还不会自动带入先前的讨论。'); return;
    }
    lock.current = true; setSubmitting(true); setError('');
    const generation = owner.current;
    const sourceMessageId = initialSource?.sessionId === session.id ? initialSource.messageId : undefined;
    const signature = JSON.stringify([session.id, sourceMessageId, intent, message, executionRoots, acceptance.trim()]);
    if (attempt.current?.signature !== signature) attempt.current = { signature, id: `primary-${crypto.randomUUID()}` };
    const clientMessageId = attempt.current.id;
    try {
      let target = session;
      if (intent === 'execute') {
        const result = await transport.request<Record<string, unknown>>({ pathId: 'agent.primary.tasks.create', body: {
          clientRequestId: clientMessageId, sourceSessionId: session.id, objective: message,
          ...(sourceMessageId ? { sourceMessageId } : {}),
          acceptanceCriteria: acceptance.split('\n').map(line => line.trim()).filter(Boolean),
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
      onOpen(target, { clientMessageId, message });
    } catch (reason) {
      if (generation === owner.current) setError(`${publicAgentErrorText(reason)} 草稿已保留；重试会核对同一次请求。`);
    } finally {
      if (generation === owner.current) { lock.current = false; setSubmitting(false); }
    }
  }

  async function chooseWorkspace(forDiscussion = false) {
    if (!transport.pickFiles) return;
    const generation = owner.current;
    try {
      const files = await transport.pickFiles({ purpose: 'workspace-root', selection: 'directory', multiple: false, maxFiles: 1 });
      if (generation !== owner.current) return;
      const path = files[0]?.path;
      if (path) { setWorkspace(path); setScopeConfirmed(false); if (forDiscussion) setContextWorkspace(path); }
    } catch (reason) { if (generation === owner.current) setError(publicAgentErrorText(reason)); }
  }

  return <div className="paw-primary-home">
    <div className="paw-primary-home__body">
      <header className="paw-primary-home__heading">
        <span className="paw-primary-home__eyebrow"><span aria-hidden="true" /> YOUR PERSONAL WORKBENCH</span>
        {transport.kind === 'mock' ? <span className="paw-primary-home__demo">演示模式 · 合成数据，不调用真实模型</span> : null}
        <h1>有事，接着聊。</h1>
        <p>{identity.assistantName} 会沿着同一段对话，记住背景，陪你把事情做完。</p>
      </header>
      <section className="paw-primary-home__composer" aria-label="我的长期助手">
        <div className="paw-primary-home__identity"><span className="paw-primary-avatar"><Sparkles aria-hidden="true" size={20} /></span><span><strong>我的助手</strong><small>{loading ? '正在读取长期对话…' : session ? '长期对话 · 先讨论，再决定行动' : '对话暂未连接'}</small></span>
          {session ? <button disabled={submitting || loading} onClick={() => onOpen(session)} onPointerEnter={() => warmAgentWorkspace('session')} type="button">打开对话 <ArrowUpRight size={14} /></button> : null}
        </div>
        <textarea aria-label="和我的助手聊聊" ref={input} value={draft} disabled={submitting} onChange={event => setDraft(event.target.value)} onKeyDown={event => {
          if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void submit(); }
        }} placeholder={intent === 'discuss' ? '想法、问题，或一件还没想清楚的事…' : '这次要完成什么？'} rows={3} />
        {intent === 'execute' ? <div className="paw-primary-home__scope">
          <p className="paw-primary-home__scope-note">本次任务使用这里填写的目标、标准和目录，还不会自动带入之前的讨论。请在上方写清要做的事和必要背景。</p>
          <label>完成标准 <span>可选，每行一项</span><textarea aria-label="完成标准" value={acceptance} disabled={submitting} onChange={event => setAcceptance(event.target.value)} placeholder="例如：测试通过，并说明修改了什么" rows={2} /></label>
          <label>本次工作目录<div className="paw-primary-home__folder"><Folder aria-hidden="true" size={15} /><input aria-label="本次工作目录" value={workspace} disabled={submitting} onChange={event => { setWorkspace(event.target.value); setScopeConfirmed(false); }} placeholder="/path/to/project" />{transport.pickFiles ? <button disabled={submitting} onClick={() => void chooseWorkspace()} type="button">选择目录</button> : null}</div></label>
          {executionRoots.length > 1 ? <ul className="paw-primary-home__root-list" aria-label="本次授权目录">{executionRoots.map(root => <li key={root}>{root}</li>)}</ul> : null}
          <label className="paw-primary-home__consent"><input type="checkbox" checked={scopeConfirmed} disabled={submitting || !workspace.trim()} onChange={event => setScopeConfirmed(event.target.checked)} /><span>允许助手在{executionRoots.length > 1 ? `以上 ${executionRoots.length} 个目录` : '这个目录'}内执行本次任务、修改文件和运行命令。可以随时停止。</span></label>
        </div> : null}
        <footer><div className="paw-primary-home__intent" role="group" aria-label="本次意图"><button aria-pressed={intent === 'discuss'} disabled={submitting} onClick={() => { setIntent('discuss'); setError(''); }} type="button"><MessageCircle size={14} />聊一聊</button><button aria-pressed={intent === 'execute'} disabled={submitting} onClick={() => { setIntent('execute'); setError(''); }} type="button"><Check size={14} />交给助手做</button></div><button aria-label={intent === 'discuss' ? '发送给我的助手' : '授权并开始任务'} className="paw-primary-home__send" disabled={loading || submitting || !session || !draft.trim() || (intent === 'execute' && (!workspace.trim() || !scopeConfirmed))} onClick={() => void submit()} type="button">{submitting ? <LoaderCircle className="ui-spin" size={17} /> : <ArrowUp size={17} />}</button></footer>
      </section>
      {intent === 'discuss' ? <div className="paw-primary-home__project"><Folder aria-hidden="true" size={13} /><select aria-label="讨论项目" disabled={submitting || loading} value={contextWorkspace} onChange={event => { setContextWorkspace(event.target.value); setWorkspace(event.target.value); setScopeConfirmed(false); }}><option value="">日常对话 · 不绑定项目</option>{Array.from(new Set([...projectRoots, ...(contextWorkspace ? [contextWorkspace] : [])])).map(root => <option key={root} value={root}>{root}</option>)}</select>{transport.pickFiles ? <button disabled={submitting || loading} onClick={() => void chooseWorkspace(true)} type="button">选择项目</button> : null}</div> : null}
      <div className="paw-primary-home__hint"><span>{intent === 'discuss' ? '当前只讨论和查阅，不授予写入或命令执行权限。' : '任务会保留在独立对话中；回来聊别的，也不会丢失进度。'}</span>{desktop ? <button onClick={() => openPawOsRoute(desktop, '/memory?view=profile')} type="button"><BookOpen size={13} />关于我</button> : null}</div>
      {error ? <div className="paw-primary-home__error" role="alert">{error}{!session && !loading ? <button onClick={() => setRevision(value => value + 1)} type="button">重新连接</button> : null}</div> : null}
      <section className="paw-primary-home__tasks" aria-label="助手的任务"><header><h2>接着做</h2><span>{tasks.length ? `${tasks.length} 个任务` : '一件事，一段清楚的记录'}</span></header>{tasks.length ? <ul>{(allTasks ? tasks : tasks.slice(0, 4)).map(task => <li key={task.id}><button onClick={() => onOpen(task)} onPointerEnter={() => warmAgentWorkspace('session')} type="button"><span><strong>{task.title}</strong><small>{taskStatus(task)}{task.lastMessagePreview ? ` · ${task.lastMessagePreview}` : ''}</small></span><ChevronRight size={16} /></button></li>)}</ul> : <p>交给助手的工作会出现在这里，过程、结果和停止入口都在任务里。</p>}{tasks.length > 4 ? <button className="paw-primary-home__advanced" onClick={() => setAllTasks(value => !value)} type="button">{allTasks ? '收起任务' : `查看全部 ${tasks.length} 个任务`}</button> : null}</section>
      <button className="paw-primary-home__advanced" onClick={onAdvanced} disabled={submitting} type="button">新建独立 Session 或多人 Room <ChevronRight size={13} /></button>
    </div>
  </div>;
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
