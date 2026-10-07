import { ArrowUpRight, BookOpen, Check, ChevronDown, Circle, CirclePause, FileText, ListChecks, MessageCircle, MoreHorizontal, Network, Orbit, Plus, RefreshCw, Scan, Square, X } from 'lucide-react';
import { lazy, Suspense, useCallback, useEffect, useId, useRef, useState } from 'react';
import type { AgentProjectionState } from '@/contracts/agent-reducer';
import type { JsonValue } from '@/platform/transport';
import { useControlTransport } from '@/app/control-transport';
import { publicAgentErrorText } from '@/features/agent/public-error';
import { roleItems, sessionItems, type SessionSummary } from '@/features/agent/types';
import { agentSessionAddress, latestActiveAgentTurnId, selectAgentProjection, useAgentLiveStore } from '@/features/agent/state/live-store';
import { useAgentLiveSession, type AgentRecoveryState } from '@/features/agent/runtime/use-agent-live-session';
import { useRoomLiveSession } from '@/features/rooms/runtime/use-room-live-session';
import { useRoomLiveStore } from '@/features/rooms/state/live-store';
import { selectActivePublicRoomTurn, selectPublicRoomTurnOrder } from '@/features/rooms/runtime/room-execution-lanes';
import type { RoomSummary } from '@/features/rooms/room-types';
import { usePawOsAppActive, usePawOsDesktop, openPawOsRoute } from '@/features/paw-os/surface-context';
import { usePresentationMotion } from '@/features/conversation-ui/reading/reading-preferences';
import { usePageVisibility } from '@/platform/use-page-visibility';
import { Dialog, DialogContent, DialogHeader, DialogTitle, FocusScope, Popover, PopoverContent, PopoverTrigger } from '@/components/primitives';
import { controlledSessionDetail } from './ControlledSessions';
import { loadSessionWorkspace } from './agent-workspace-loader';
import { RoomPlanetAvatar, type RoomPlanetActivity } from '@/features/rooms/RoomPlanetAvatar';
import { ChatPresentationProvider, useChatPresentation } from '@/features/conversation-ui/reading/chat-presentation';
import { appendWorkspaceRecoveryDraft } from '@/features/semantic-workspace/workspace-recovery';
import { ChatPresentationSettings } from '@/features/conversation-ui/reading/ChatPresentationSettings';
import { ControlledRoomParticipants } from '@/features/agent/collaboration/ControlledRoomParticipants';
import type { ScreenAssistantHost } from '@/features/screen-assistant/screen-assistant-model';
import './coordinator-app.css';

const SessionWorkspace = lazy(loadSessionWorkspace);
export type CoordinatorObject = { kind: 'session' | 'room'; id: string; coordinatorId: string; sourceSessionId: string; task: string; target: SessionSummary | RoomSummary; outputs?: { reference: string; title: string; sessionId: string }[] };
export function coordinatorObjects(value: unknown, source: string, identity: string): CoordinatorObject[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is CoordinatorObject => {
    if (!item || typeof item !== 'object') return false;
    const row = item as Partial<CoordinatorObject>;
    return (row.kind === 'session' || row.kind === 'room') && row.sourceSessionId === source && row.coordinatorId === identity
      && typeof row.id === 'string' && typeof row.task === 'string' && row.target?.id === row.id && typeof row.target.title === 'string'
      && row.target.status !== 'archived';
  });
}

/** Ready bootstrap may create an empty completed reducer turn. Skip only
 * that harmless boundary; newer admitted/activity turns retain their outcome. */
export function coordinatorLatestTurnOutcome(current?: AgentProjectionState) {
  if (!current) return undefined;
  for (let index = current.turnOrder.length - 1; index >= 0; index -= 1) {
    const turn = current.turnsById[current.turnOrder[index]];
    if (!turn) continue;
    const hasConversationMessage = turn.messageIds.some(id => {
      const message = current.messagesById[id];
      return message && (message.role === 'user' || message.role === 'assistant');
    });
    if (hasConversationMessage || turn.activityIds.length > 0 || turn.status !== 'completed') return turn.status;
  }
  return undefined;
}

/** One persistent ordinary Pi conversation, plus explicitly owned targets. */
export function PawCoordinatorApp() {
  return <ChatPresentationProvider ownerKey="builtin:agent-controller" defaultVersion="v2"><PawCoordinatorAppBody/></ChatPresentationProvider>;
}
function PawCoordinatorAppBody() {
  const presentation = useChatPresentation();
  const transport = useControlTransport();
  const desktop = usePawOsDesktop();
  const surfaceActive = usePawOsAppActive() ?? true;
  const pageVisible = usePageVisibility();
  const [record, setRecord] = useState<SessionSummary>();
  const [identity, setIdentity] = useState('');
  const [objects, setObjects] = useState<CoordinatorObject[]>([]);
  const [error, setError] = useState('');
  const [reading, setReading] = useState(true);
  const [railOpen, setRailOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [captureBusy, setCaptureBusy] = useState(false);
  const capturePending = useRef(false);
  const [captureNotice, setCaptureNotice] = useState('');
  const host: ScreenAssistantHost | undefined = typeof window === 'undefined' ? undefined : window.pawScreenAssistant;
  const companionHost = typeof window === 'undefined' ? undefined : window.pawDesktopCompanion;
  const companionPending = useRef(false);
  const [companionBusy, setCompanionBusy] = useState(false);
  const [companionNotice, setCompanionNotice] = useState('');
  const [sourceSynced, setSourceSynced] = useState(false);
  const [sourceRecovery, setSourceRecovery] = useState<AgentRecoveryState>('recovering');
  const sourceProjection = useAgentLiveStore(state => selectAgentProjection(state, agentSessionAddress(transport, record?.id ?? '')));
  useEffect(() => { setSourceSynced(false); setSourceRecovery('recovering'); }, [transport, record?.id]);
  useAgentLiveSession({ sessionId: record?.id ?? '', transport, active: Boolean(record) && surfaceActive && pageVisible, snapshotView: 'recent', onSnapshot: () => setSourceSynced(true), onRecoveryState: setSourceRecovery });
  const sourceCurrent = surfaceActive && pageVisible && sourceSynced && sourceRecovery === 'synced' && !sourceProjection?.needsSnapshot ? sourceProjection : undefined;
  const capture = async () => {
    if (!host || !record || capturePending.current) return;
    capturePending.current = true; setCaptureBusy(true); setCaptureNotice('正在打开系统框选…');
    try { const opened = await host.capture({ sourceSessionId: record.id }); setCaptureNotice(opened ? '框选窗口已打开，在选区对话中继续。' : '框选未打开，可以再次尝试。'); }
    catch (reason) { setCaptureNotice(publicAgentErrorText(reason, '框选未能打开，请重试。')); }
    finally { capturePending.current = false; setCaptureBusy(false); }
  };
  const showCompanion = async () => {
    if (!companionHost || companionPending.current) return;
    companionPending.current = true; setCompanionBusy(true); setCompanionNotice('正在打开桌面行星…');
    try { const shown = await companionHost.show(); setCompanionNotice(shown ? '桌面行星已显示。' : '桌面行星未能打开，可以再次尝试。'); }
    catch (reason) { setCompanionNotice(publicAgentErrorText(reason, '桌面行星未能打开，请重试。')); }
    finally { companionPending.current = false; setCompanionBusy(false); }
  };
  const railOpenRef = useRef(railOpen); railOpenRef.current = railOpen;
  const readingSurface = useRef<HTMLDivElement>(null);
  const [creation, setCreation] = useState<'session' | 'room'>();
  const [limit, setLimit] = useState(4);
  const [outputLimit, setOutputLimit] = useState(3);
  const generation = useRef(0);
  const railToggle = useRef<HTMLButtonElement>(null);
  const railClose = useRef<HTMLButtonElement>(null);
  const restoreRailFocus = useRef(false);
  const closeRail = useCallback(() => { restoreRailFocus.current = true; setRailOpen(false); }, []);
  useEffect(() => {
    if (railOpen) { railClose.current?.focus(); return; }
    if (!restoreRailFocus.current) return;
    restoreRailFocus.current = false;
    // Release FocusScope before returning focus to the trigger.
    const frame = requestAnimationFrame(() => railToggle.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [railOpen]);
  const refresh = useCallback(async () => {
    const sequence = ++generation.current;
    setReading(true);
    try {
      const response = await transport.request<Record<string, unknown>>({ pathId: 'agent.coordinator.ensure', body: {} });
      if (sequence !== generation.current) return;
      const session = sessionItems({ items: [response.session] })[0];
      if (!session || typeof response.coordinatorId !== 'string') throw new Error('Agent 会话尚未确认');
      setRecord(session); setIdentity(response.coordinatorId);
      setObjects(coordinatorObjects(response.objects, session.id, response.coordinatorId)); setError('');
    } catch (reason) { if (sequence === generation.current) setError(publicAgentErrorText(reason, '暂时无法连接 Agent')); }
    finally { if (sequence === generation.current) setReading(false); }
  }, [transport]);
  useEffect(() => { setRecord(undefined); setObjects([]); void refresh(); return () => { generation.current++; }; }, [refresh]);
  useEffect(() => {
    if (!record || !surfaceActive || !pageVisible) return;
    // Directory refresh discovers targets created by the Agent's tool. Runtime
    // status comes from each target's existing live controller, never this timer.
    const timer = window.setInterval(() => { void refresh(); }, 15000);
    return () => window.clearInterval(timer);
  }, [record?.id, surfaceActive, pageVisible, refresh]);
  useEffect(() => {
    const surface = readingSurface.current;
    if (!surface) return;
    let timeline: HTMLElement | null = null;
    const resize = new ResizeObserver(() => {
      if (timeline) surface.style.setProperty('--coordinator-scrollbar-width', `${Math.max(0, timeline.offsetWidth - timeline.clientWidth)}px`);
    });
    const attach = () => {
      const next = surface.querySelector<HTMLElement>('.agent-timeline [data-virtuoso-scroller]');
      if (next === timeline) return;
      if (timeline) resize.unobserve(timeline);
      timeline = next;
      if (timeline) {
        surface.style.setProperty('--coordinator-scrollbar-width', `${Math.max(0, timeline.offsetWidth - timeline.clientWidth)}px`);
        resize.observe(timeline);
      }
    };
    const mutation = new MutationObserver(attach);
    mutation.observe(surface, { childList: true, subtree: true }); attach();
    return () => { resize.disconnect(); mutation.disconnect(); };
  }, [record?.id]);
  const command = useCallback(async (action: string, targetId: string, input: { [key: string]: JsonValue }) => {
    if (!record) return;
    const response = await transport.request<Record<string, unknown>>({ pathId: 'agent.coordinator.command', body: {
      sourceSessionId: record.id, action, targetId, input,
    } });
    if (response.ok !== true) throw new Error('操作尚未确认，请查看原会话');
  }, [record?.id, transport]);
  const open = useCallback((object: CoordinatorObject) => { setRailOpen(false); desktop?.openWindow({ appId: 'agent', target: {
    kind: object.kind, id: object.id, title: object.target.title,
  } }); }, [desktop]);
  const openParticipant = useCallback((id: string, title: string) => {
    setRailOpen(false);
    desktop?.openWindow({ appId: 'agent', target: { kind: 'session', id, title } });
  }, [desktop]);
  const openSource = useCallback(() => {
    if (!record || !desktop) return;
    setRailOpen(false);
    desktop.openWindow({ appId: 'agent', target: { kind: 'session', id: record.id, title: record.title } });
  }, [desktop, record]);
  const outputs = objects.flatMap(object => (object.outputs ?? []).filter(output => typeof output.reference === 'string' && typeof output.title === 'string' && typeof output.sessionId === 'string')
    .map((output, index) => ({ object, output, key: `${object.id}:${index}` })));
  const openOutput = (object: CoordinatorObject, output: { reference: string; sessionId: string }) => {
    // A shared Room artifact need not be authored by a participant. Read it
    // through this owning source Session; Files keeps its normal access checks.
    const sessionId = output.sessionId || (object.kind === 'room' && output.reference.startsWith('/') ? record?.id : '');
    if (desktop && sessionId && !/^[a-z]+:/iu.test(output.reference)) {
      setRailOpen(false);
      openPawOsRoute(desktop, `/files?session=${encodeURIComponent(sessionId)}&path=${encodeURIComponent(output.reference)}`);
    } else open(object);
  };
  return <div ref={readingSurface} className="paw-coordinator" data-rail-open={railOpen} data-chat-presentation-version={presentation?.version}>
    <main className="paw-coordinator__conversation">
      <header className="paw-coordinator__header"><div><h1><RoomPlanetAvatar variant={presentation?.version === 'v2' ? 'sphere' : 'classic'} ordinal={0} size={24} decorative activity="static"/>星伴</h1><span>持续对话 <i aria-hidden="true">·</i> {record?.executionMode === 'full_trust' ? '全盘访问' : record?.executionMode === 'read_only' ? '只读' : '按当前权限执行'}</span></div>
        <div className="paw-coordinator__toolbar">
          <Popover><PopoverTrigger asChild><button type="button" aria-label="屏幕工具"><Scan size={16}/><span>屏幕工具</span></button></PopoverTrigger><PopoverContent align="end" className="paw-coordinator__screen-tools" aria-label="屏幕工具">
            <strong>围绕选区继续对话</strong><p>在框选窗口中翻译、解释、整理笔记或操作电脑。</p>
            <button type="button" onClick={() => { void capture(); }} disabled={!host || !record || captureBusy} aria-busy={captureBusy}><Scan size={16}/>框选屏幕</button>
            <p role="status">{captureNotice || (!host ? '请在 PAW 桌面宿主中使用框选。' : '框选在独立选区对话中继续，不会自动发送。')}</p>
          </PopoverContent></Popover>
          <button type="button" aria-label="管理长期记忆" disabled={!desktop} onClick={() => openPawOsRoute(desktop, '/memory')}><BookOpen size={16}/><span>记忆</span></button>
        <button type="button" ref={railToggle} className="paw-coordinator__rail-toggle" aria-expanded={railOpen} aria-controls="coordinator-controls" onClick={() => setRailOpen(!railOpen)}><Network size={16}/>控制 <span>{objects.length}</span></button></div>
      </header>
      {error ? <div role="alert" className="paw-coordinator__error">{error}<button type="button" onClick={() => { void refresh(); }}>重新连接</button></div> : null}
      {record ? <Suspense fallback={<p className="paw-coordinator__loading">正在打开对话…</p>}><SessionWorkspace key={record.id} recordId={record.id} record={record} active={surfaceActive}
        appearance="embedded" showComposerControls composerPlaceholder="与星伴继续对话…"
        onNewWork={() => setCreation('session')} onSessionCreated={(session, draft) => {
          if (!appendWorkspaceRecoveryDraft(transport, `session:${session.id}`, draft)) setError('分支已建立，分支草稿暂时无法保存到本机，请复制保留。');
          desktop?.openWindow({ appId: 'agent', target: { kind: 'session', id: session.id, title: session.title } });
        }}
        onSessionActivity={() => { void refresh(); }} onSessionUpdated={setRecord} /></Suspense>
        : <p className="paw-coordinator__loading">{reading ? '正在连接持久对话…' : '对话未连接，重新连接后继续。'}</p>}
    </main>
    <FocusScope asChild trapped={railOpen && surfaceActive} loop={railOpen} onMountAutoFocus={event => event.preventDefault()} onUnmountAutoFocus={event => event.preventDefault()}><aside onKeyDown={event => { if (event.key === 'Escape' && railOpen && !settingsOpen) { event.preventDefault(); closeRail(); } }} id="coordinator-controls" className="paw-coordinator__rail" aria-label="Agent 控制的 Session 与 Room">
      <header className="paw-coordinator__identity"><CoordinatorIdentityStatus session={record} current={sourceCurrent} recovery={sourceRecovery} onOpen={record && desktop ? openSource : undefined}/><Popover open={settingsOpen} onOpenChange={setSettingsOpen}><PopoverTrigger asChild><button type="button" className="paw-coordinator__settings-trigger" aria-label="Agent 显示设置"><MoreHorizontal size={17}/></button></PopoverTrigger><PopoverContent align="end" className="paw-coordinator__settings" aria-label="Agent 显示设置" onCloseAutoFocus={event => { if (!railOpenRef.current) event.preventDefault(); }}>
        <ChatPresentationSettings/>
        {companionHost ? <div className="paw-coordinator__companion-entry"><button type="button" disabled={companionBusy} aria-busy={companionBusy} onClick={() => { void showCompanion(); }}><Orbit size={16}/>显示桌面行星</button>{companionNotice ? <p role="status">{companionNotice}</p> : null}</div> : null}
      </PopoverContent></Popover><button type="button" ref={railClose} className="paw-coordinator__rail-close" aria-label="收起控制面板" onClick={() => { closeRail(); }}><X size={16}/></button></header>
      <CoordinatorProgress current={sourceCurrent} connected={Boolean(record)}/>
      <details className="paw-coordinator__rail-section" open><summary><Network size={15}/><strong>Session 与 Room</strong><span>{objects.length}</span><ChevronDown size={14}/></summary>
      <div className="paw-coordinator__section-heading"><h2 className="paw-coordinator__sr-only">Sessions & Rooms <span>{objects.length}</span></h2><button type="button" aria-label="刷新控制对象" disabled={reading} onClick={() => { void refresh(); }}><RefreshCw size={14}/></button></div>
      <div className="paw-coordinator__create"><button type="button" disabled={!record || record.executionMode !== 'full_trust'} onClick={() => setCreation('session')}><Plus size={14}/>Session</button><button type="button" disabled={!record || record.executionMode !== 'full_trust'} onClick={() => setCreation('room')}><Plus size={14}/>Room</button></div>
      {!objects.length ? <p className="paw-coordinator__empty">交给 Agent 的工作会出现在这里。也可以先创建一个 Session 或 Room。</p> : null}
      <div className="paw-coordinator__objects">{objects.slice(0, limit).map(object => object.kind === 'session'
        ? <CoordinatorSession key={object.id} object={object} active={surfaceActive && pageVisible} onOpen={open} command={command}/>
        : <CoordinatorRoom key={object.id} object={object} active={surfaceActive && pageVisible} onOpen={open} onOpenParticipant={desktop ? openParticipant : undefined} command={command}/>)}</div>
      {objects.length > limit ? <button className="paw-coordinator__more" type="button" onClick={() => setLimit(limit + 4)}>显示更多 <ChevronDown size={14}/></button> : limit > 4 ? <button className="paw-coordinator__more" type="button" onClick={() => setLimit(4)}>收起</button> : null}
      </details>
      <details className="paw-coordinator__rail-section paw-coordinator__outputs-section" open={outputs.length > 0}><summary><FileText size={15}/><strong>产物</strong><span>{outputs.length}</span><ChevronDown size={14}/></summary>
      {!outputs.length ? <p className="paw-coordinator__empty">还没有可打开的产物。</p> : outputs.slice(0, outputLimit).map(({ object, output, key }) => <button type="button" className="paw-coordinator__output" key={key} onClick={() => openOutput(object, output)} title={output.reference}><FileText size={15}/><span>{output.title || output.reference.split('/').at(-1)}<small>{object.target.title}</small></span><ArrowUpRight size={13}/></button>)}
      {outputs.length > outputLimit ? <button type="button" className="paw-coordinator__more" onClick={() => setOutputLimit(outputLimit + 3)}>显示更多产物</button> : null}</details>
    </aside></FocusScope>
    {creation && record ? <CoordinatorCreate kind={creation} sourceId={record.id} onClose={() => setCreation(undefined)} onCreated={() => { setCreation(undefined); void refresh(); }} /> : null}
  </div>;
}

function CoordinatorIdentityStatus({ session, current, recovery, onOpen }: { session?: SessionSummary; current?: AgentProjectionState; recovery: AgentRecoveryState; onOpen?: () => void }) {
  const statusId = useId();
  const presentation = useChatPresentation();
  const outcome = coordinatorLatestTurnOutcome(current);
  const status = !current ? recovery === 'failed' ? '同步失败' : '正在同步' : ['busy', 'analyzing', 'working', 'retrying'].includes(current.status) ? '正在处理工作' : current.status === 'waiting' ? '等待输入' : ['aborting','stopping'].includes(current.status) ? '正在停止' : ['faulted','failed'].includes(current.status) || outcome === 'failed' ? '需要查看' : outcome === 'aborted' ? '已停止' : outcome === 'completed' ? '本轮已完成' : '就绪';
  const activity: RoomPlanetActivity = !current ? 'static' : ['busy','analyzing','working','retrying'].includes(current.status) ? 'working'
    : current.status === 'waiting' ? 'waiting' : ['failed','faulted'].includes(current.status) || outcome === 'failed' ? 'error'
    : outcome === 'aborted' || ['aborting','stopping'].includes(current.status) ? 'stopped'
    : outcome === 'completed' ? 'done' : 'static';
  return <><button type="button" className="paw-coordinator__avatar" aria-label="打开 Agent 的原 Session" aria-describedby={statusId} title="打开这段持久对话的完整 Session" disabled={!session || !onOpen} onClick={onOpen}><RoomPlanetAvatar variant={presentation?.version === 'v2' ? 'sphere' : 'classic'} ordinal={0} size={46} decorative activity={presentation?.version === 'v2' && current && activity === 'static' ? 'idle' : activity} interactive={Boolean(current)}/></button>
    <div><strong title={session?.id}>{session?.title ?? 'Agent'}</strong><small id={statusId} aria-live="polite">{status}</small></div></>;
}
export function CoordinatorProgress({ current, connected }: { current?: AgentProjectionState; connected: boolean }) {
  const tasks = current?.todo.phases.flatMap((phase, phaseIndex) => phase.tasks.map((task, index) => ({ ...task, key: `${phaseIndex}:${index}` }))) ?? [];
  if (current && !tasks.length || !connected) return null;
  const done = tasks.filter(task => task.status === 'completed').length;
  const labels = { pending: '待处理', in_progress: '进行中', blocked: '受阻', completed: '已完成', abandoned: '已放弃' };
  return <details className="paw-coordinator__rail-section paw-coordinator__progress" open>
    <summary><ListChecks size={15}/><strong>当前进度</strong><span>{current ? `${done}/${tasks.length}` : '未同步'}</span><ChevronDown size={14}/></summary>
    {!current ? <p className="paw-coordinator__empty">同步后显示这段对话的实际 Todo。</p> : <ul>{tasks.map(task => <li key={task.key} data-state={task.status}>
      {task.status === 'completed' ? <Check size={14}/> : task.status === 'blocked' ? <CirclePause size={14}/> : <Circle size={14}/>}
      <div><span>{task.content}</span><small>{labels[task.status]}{task.reason ? ` · ${task.reason}` : ''}</small></div>
    </li>)}</ul>}
  </details>;
}
function CoordinatorSession({ object, active, onOpen, command }: ObjectRowProps) {
  const transport = useControlTransport();
  const session = object.target as SessionSummary;
  const [synced, setSynced] = useState(false);
  const [recovery, setRecovery] = useState<AgentRecoveryState>('recovering');
  const projection = useAgentLiveStore(state => selectAgentProjection(state, agentSessionAddress(transport, object.id)));
  const reload = useAgentLiveSession({ sessionId: object.id, transport, active, snapshotView: 'recent',
    onSnapshot: () => setSynced(true), onRecoveryState: setRecovery });
  const current = active && synced && recovery === 'synced' && !projection?.needsSnapshot ? projection : undefined;
  const turnId = current && latestActiveAgentTurnId(current);
  const original = current?.durableRecovery?.activeTurn;
  const turn = current && turnId ? current.turnsById[turnId] : undefined;
  const outcome = coordinatorLatestTurnOutcome(current);
  const running = Boolean(current && turn?.status === 'running' && !current.durableRecovery?.paused
    && !['waiting', 'aborting', 'stopping'].includes(current.status));
  const userMessage = current && turn?.messageIds.map(id => current.messagesById[id]).find(message => message?.role === 'user');
  const clientMessageId = original?.clientMessageId || userMessage?.clientMessageId;
  const exact = turnId && clientMessageId ? { turnId, clientMessageId } : undefined;
  const status = !current ? recovery === 'failed' ? '同步失败' : '正在同步' : current.durableRecovery?.paused ? '已暂停'
    : ['aborting', 'stopping'].includes(current.status) ? '正在停止' : running ? '正在执行' : turn?.status === 'waiting' || current.status === 'waiting' ? '等待输入'
    : ['failed', 'faulted'].includes(current.status) ? '需要查看' : (current.goal?.status === 'completed' || session.goal?.status === 'completed') ? '已完成' : outcome === 'aborted' ? '已停止' : outcome === 'failed' ? '本轮失败' : outcome === 'completed' ? '本轮已完成' : '就绪';
  const stop = exact && current && ['running', 'waiting'].includes(turn?.status ?? '') ? async () => { await command('stop', object.id, exact); await reload({ preserveAfterSequence: current.lastSequence }); } : undefined;
  const resume = current?.runtimeEngine === 'durable' && current.durableRecovery?.paused && original && !current.durableRecovery.compactionTarget
    ? async () => { await command('resume', object.id, original); await reload({ preserveAfterSequence: current.lastSequence }); } : undefined;
  return <CoordinatorRow object={object} detail={controlledSessionDetail(session) || (object.task.startsWith(session.title.trim().replace(/(?:…|\.\.\.)$/u, '')) ? '' : object.task)} status={status} running={running} stop={stop} resume={resume} onOpen={onOpen}/>;
}
function CoordinatorRoom({ object, active, onOpen, onOpenParticipant, command }: ObjectRowProps) {
  const transport = useControlTransport();
  const [recovery, setRecovery] = useState<AgentRecoveryState>('recovering');
  const [synced, setSynced] = useState(false);
  const projection = useRoomLiveStore(state => state.projections[object.id]);
  const retry = useRoomLiveSession({ roomId: object.id, transport, active,
    onLoadingChange: () => undefined, onSnapshot: () => setSynced(true), onMetadata: () => undefined,
    onConnectionRestored: () => undefined, onConnectionError: () => undefined, onRecoveryState: (_id, state) => setRecovery(state), onEvents: () => undefined });
  const current = active && synced && recovery === 'synced' && !projection?.needsSnapshot ? projection : undefined;
  const turn = current && selectActivePublicRoomTurn(current);
  const latest = current && current.turnsById[selectPublicRoomTurnOrder(current).at(-1) ?? ''];
  const running = Boolean(turn?.status === 'running');
  const status = !current ? recovery === 'failed' ? '同步失败' : '正在同步' : running ? '伙伴正在执行'
    : turn?.status === 'queued' ? '等待调度' : latest?.status === 'failed' ? '需要查看' : latest?.status === 'completed' ? '已完成' : latest?.status === 'aborted' ? '已停止' : 'Room 已准备好';
  const stop = turn ? async () => { await command('stop', object.id, { roomTurnId: turn.id, clientRequestId: crypto.randomUUID() }); retry(); } : undefined;
  return <div><CoordinatorRow object={object} detail={object.task} status={status} running={running} stop={stop} onOpen={onOpen}/><ControlledRoomParticipants room={object.target as RoomSummary} active={active} onOpenSession={onOpenParticipant}/></div>;
}
type ObjectRowProps = { object: CoordinatorObject; active: boolean; onOpenParticipant?: (id: string, title: string) => void; onOpen: (object: CoordinatorObject) => void; command: (action: string, id: string, input: { [key: string]: JsonValue }) => Promise<void> };
function CoordinatorRow({ object, detail, status, running, stop, resume, onOpen }: { object: CoordinatorObject; detail: string; status: string; running: boolean; stop?: () => Promise<void>; resume?: () => Promise<void>; onOpen: ObjectRowProps['onOpen'] }) {
  const motion = usePresentationMotion();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const lock = useRef(false);
  async function invoke(action: () => Promise<void>) {
    if (lock.current) return; lock.current = true; setPending(true); setError('');
    try { await action(); } catch (reason) { setError(publicAgentErrorText(reason, '操作尚未确认')); }
    finally { lock.current = false; setPending(false); }
  }
  return <div className="paw-coordinator__object" data-control-active={running} data-motion={motion ? 'active' : 'paused'}>
    <span className="paw-coordinator__connection" aria-hidden="true"><i/></span>
    <button type="button" className="paw-coordinator__object-open" onClick={() => onOpen(object)} aria-label={`打开 ${object.kind} ${object.target.title}`}>
      {object.kind === 'session' ? <MessageCircle size={16}/> : <Network size={16}/>}<span><strong>{object.target.title}</strong>{detail && detail !== object.target.title ? <small>{detail}</small> : null}<em>{object.kind === 'room' ? 'Room' : 'Session'} <i>·</i> {status}</em></span><ArrowUpRight size={13}/>
    </button>
    {stop || resume ? <div className="paw-coordinator__actions">{stop ? <button type="button" disabled={pending} aria-busy={pending} onClick={() => { void invoke(stop); }}><Square size={11}/>停止</button> : null}{resume ? <button type="button" disabled={pending} onClick={() => { void invoke(resume); }}>继续</button> : null}</div> : null}
    {error ? <p role="alert">{error}</p> : null}
  </div>;
}
function CoordinatorCreate({ kind, sourceId, onClose, onCreated }: { kind: 'session' | 'room'; sourceId: string; onClose: () => void; onCreated: () => void }) {
  const transport = useControlTransport();
  const [task, setTask] = useState('');
  const [personas, setPersonas] = useState<ReturnType<typeof roleItems>>([]);
  const [partners, setPartners] = useState<string[]>([]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const lock = useRef(false);
  const attempt = useRef<{ signature: string; id: string } | undefined>(undefined);
  useEffect(() => { if (kind !== 'room') return; const controller = new AbortController();
    void transport.request({ pathId: 'agent.roles.list', signal: controller.signal }).then(response => {
      if (controller.signal.aborted) return; const values = roleItems(response); setPersonas(values); setPartners(values.slice(0, 2).map(item => item.roleId));
    }).catch(reason => { if (!controller.signal.aborted) setError(publicAgentErrorText(reason, '伙伴配置未连接')); });
    return () => controller.abort();
  }, [kind, transport]);
  async function create() {
    if (lock.current || !task.trim() || kind === 'room' && (partners.length !== 2 || partners[0] === partners[1])) return;
    const input = { task: task.trim(), ...(kind === 'room' ? { participants: partners.map((roleId, index) => ({ roleId, collaborationRole: index ? 'reviewer' : 'coordinator' })) } : {}) };
    const signature = JSON.stringify(input);
    if (attempt.current?.signature !== signature) attempt.current = { signature, id: crypto.randomUUID() };
    lock.current = true; setPending(true); setError('');
    try { const response = await transport.request<Record<string, unknown>>({ pathId: 'agent.coordinator.command', body: {
      sourceSessionId: sourceId, action: `create_${kind}`, clientRequestId: attempt.current.id, input,
    } }); if (response.ok !== true) throw new Error('创建尚未确认'); onCreated(); }
    catch (reason) { setError(publicAgentErrorText(reason, '创建尚未确认，重试会核实同一次创建')); }
    finally { lock.current = false; setPending(false); }
  }
  return <Dialog open onOpenChange={open => { if (!open && !pending) onClose(); }}><DialogContent className="paw-coordinator__create-dialog"><DialogHeader><DialogTitle>创建 {kind === 'session' ? 'Session' : 'Room'}</DialogTitle></DialogHeader>
    <label>任务<textarea aria-label="新控制对象的任务" value={task} onChange={event => setTask(event.target.value)} maxLength={4000} rows={3}/></label>
    {kind === 'room' ? <div className="paw-coordinator__partners">{[0, 1].map(index => <label key={index}>{index ? '复核伙伴' : '协调伙伴'}<select aria-label={index ? '复核伙伴' : '协调伙伴'} value={partners[index] ?? ''} onChange={event => setPartners(values => [index ? values[0] : event.target.value, index ? event.target.value : values[1]])}>{personas.map(persona => <option key={persona.roleId} value={persona.roleId}>{persona.displayName}</option>)}</select></label>)}</div> : null}
    <p>创建后由你或 Agent 显式开始工作。</p>{error ? <p role="alert">{error}</p> : null}
    <div className="paw-coordinator__dialog-actions"><button type="button" disabled={pending} onClick={onClose}>取消</button><button type="button" disabled={pending || !task.trim() || kind === 'room' && (!partners[1] || partners[0] === partners[1])} onClick={() => { void create(); }}>{pending ? '正在创建…' : '创建'}</button></div>
  </DialogContent></Dialog>;
}
