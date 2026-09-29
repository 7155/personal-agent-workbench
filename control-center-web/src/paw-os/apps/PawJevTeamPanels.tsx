import { ArrowLeft, Check, Circle, ChevronDown, ChevronRight, CircleAlert, ExternalLink, FileText, FolderOpen, GitBranch, Layers2, ListChecks, LoaderCircle, PanelRightClose, Paperclip, Search, X } from 'lucide-react';
import { AnimatePresence, motion, useIsPresent, useReducedMotion } from 'motion/react';
import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { Button, Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/primitives';
import { selectRoomParticipantPublicProgress, type RoomProjectionState } from '@/contracts/room-reducer';
import { toolExecutionOutcome } from '@/features/conversation-ui/model/tool-receipt';
import { RoomPlanetAvatar } from '@/features/rooms/RoomPlanetAvatar';
import { roomPlanetName } from '@/features/rooms/room-copy';
import type { RoomSummary } from '@/features/rooms/room-types';
import { JevTaskAssignment } from '@/features/semantic-workspace/JevTaskAssignment';
import { JevActivityIcon } from '@/features/semantic-workspace/JevActivityIcon';
import { JevTaskRevision } from '@/features/semantic-workspace/JevTaskRevision';
import type { JevTaskControls } from '@/features/semantic-workspace/jev-task-assignment';
import { JEV_TASK_STAGE_LABELS, JEV_TASK_STAGE_LABELS as TASK_STATES, jevCurrentTaskVersion, jevLeafTasks, jevTaskStage, type JevEffect, type JevPlanTask, type JevSnapshot, type JevTask } from '@/features/semantic-workspace/jev-execution';
import { useReceiptHighlight } from '@/features/semantic-workspace/use-receipt-highlight';
import { jevMission, type JevMissionTask } from '@/features/semantic-workspace/jev-mission';
import './paw-jev-team.css';
import { JevCollaborationPanel } from './JevCollaborationPanel';
import { JevDeliveryDesk, JevDeliveryDeskLaunch } from './JevDeliveryDesk';
import { emptyDeliveryDesk, type DeliveryDeskState } from './jev-delivery-desk-model';
import { JevRailFilters, matchesJevRailFilter, type JevRailFilter } from './JevRailFilters';
import { usePresentationMotion } from '@/features/conversation-ui/reading/reading-preferences';
import { CollabTimelinePeek, CollabTimelineStage } from '@/features/collab-timeline/CollabTimelineStage';
import { roomPlanetAvatarRenderer, useRoomTimeline } from '@/features/collab-timeline/RoomCollabTimeline';

const PURPOSES: Record<string, string> = { plan: '对话与规划', execute: '执行任务', verify: '结果复核', synthesize: '汇总交付' };
const STATUS: Record<string, string> = { prepared: '等待派发', admitted: '已接收', running: '正在进行', unknown: '回执待核实' };
type Participant = RoomSummary['participants'][number];
export interface JevPartnerWork { participant: Participant; effects: JevEffect[] }

/** Current execution comes from exact task revisions and Runtime receipts. */
export function jevPartnerWork(graph: JevSnapshot | null, room?: RoomSummary): JevPartnerWork[] {
  if (!graph || !room || graph.final) return [];
  const effects = graph.effects.filter(effect => effect.operation === 'dispatch'
    && ['prepared', 'admitted', 'running', 'unknown'].includes(effect.executionStatus)
    && graph.tasks.some(task => task.id === effect.request.taskId && task.revision === effect.request.taskRevision));
  return room.participants.filter(participant => effects.some(effect => effect.request.ownerId === participant.id))
    .sort((a, b) => a.ordinal - b.ordinal)
    .map(participant => ({ participant, effects: effects.filter(effect => effect.request.ownerId === participant.id) }));
}

function rootProjection(projection: RoomProjectionState, rootId: string): RoomProjectionState {
  const turns = projection.turnOrder.filter(id => id === rootId || projection.turnsById[id]?.rootId === rootId);
  const ids = new Set(turns);
  return { ...projection, turnOrder: turns,
    messageOrder: projection.messageOrder.filter(id => ids.has(projection.messagesById[id]?.turnId)),
    activityOrder: projection.activityOrder.filter(id => ids.has(projection.activitiesById[id]?.turnId) || projection.activitiesById[id]?.payload.rootId === rootId),
  };
}

/** Reused Sessions keep history; a live card shows only its current dispatch. */
export function jevPartnerProjection(projection: RoomProjectionState, rootId: string, work: JevPartnerWork): RoomProjectionState {
  const scoped = rootProjection(projection, rootId);
  const belongs = (dispatchId: unknown, sourceTurnId: unknown, sessionId: string, participantId: string | null) => participantId === work.participant.id
    && work.effects.some(effect => {
      // A declared dispatch always wins over a turn fallback. Never pull an
      // earlier purpose into this card because it reused the same Session.
      if (typeof dispatchId === 'string' && dispatchId) return dispatchId === effect.request.dispatchId;
      return typeof sourceTurnId === 'string' && Boolean(sourceTurnId)
        && sourceTurnId === effect.receipt.turnId && Boolean(sessionId) && sessionId === effect.request.sessionId;
    });
  const messageOrder = scoped.messageOrder.filter(id => {
    const message = scoped.messagesById[id];
    return message && belongs(message.dispatchId, message.sourceTurnId, message.sourceSessionId, message.participantId);
  });
  const activityOrder = scoped.activityOrder.filter(id => {
    const activity = scoped.activitiesById[id];
    return activity && belongs(activity.payload.dispatchId, activity.payload.sourceTurnId, activity.sourceSessionId, activity.participantId);
  });
  const turnIds = new Set([...messageOrder.map(id => scoped.messagesById[id].turnId), ...activityOrder.map(id => scoped.activitiesById[id].turnId)]);
  return { ...scoped, messageOrder, activityOrder, turnOrder: scoped.turnOrder.filter(id => turnIds.has(id)) };
}

type PanelKind = 'current' | 'records' | 'plan';
type Inspector = { kind: 'deliveries' } | { kind: 'collaboration' } | { kind: 'panel'; panel: PanelKind } | { kind: 'task'; task: JevTask } | { kind: 'plan'; task: JevPlanTask };
const PANEL_TITLES: Record<PanelKind, string> = { current: '正在进行', records: '结果与交付', plan: '拟执行分工' };

/** Keep source text intact in the inspector; the rail only carries a short excerpt. */
function excerpt(value: string, limit = 72): string {
  const text = value.replace(/\s+/gu, ' ').trim();
  return text.length > limit ? `${text.slice(0, limit).trimEnd()}…` : text;
}

function taskTitle(task: Pick<JevTask, 'objective'>, graph: JevSnapshot): string {
  const proposal = graph.planApproval?.tasks.find(item => item.objective === task.objective);
  const sentence = task.objective.split(/[。；;\n]/u)[0].split(/[，,](?:routine|complex|critical|由宿主)/iu)[0]
    .replace(/^由.{1,20}?伙伴(?:独立)?/u, '').trim();
  if (sentence && sentence.length <= 40) return sentence;
  if (proposal?.key) return proposal.key.replace(/[_-]+/gu, ' ');
  return excerpt(sentence || task.objective || '任务信息待同步', 36);
}

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function executionModel(effect: JevEffect): string {
  // Preparation selects a model; only an accepted dispatch confirms it was applied.
  if (effect.state !== 'accepted' && effect.receipt.state !== 'accepted') return '';
  const selection = object(object(object(effect.request.contextManifest).executionScope).modelSelection);
  return typeof selection.modelId === 'string' ? [selection.modelId, typeof selection.thinkingLevel === 'string' ? selection.thinkingLevel : ''].filter(Boolean).join(' · ') : '';
}
function fileEvidence(tasks: JevTask[], graph: JevSnapshot | null, room?: RoomSummary) {
  const seen = new Set<string>();
  return tasks.flatMap(task => {
    const participant = room?.participants.find(item => item.id === task.ownerId);
    const producer = graph?.effects.find(effect => effect.effectId === task.acceptedTurnId
      && effect.operation === 'dispatch' && effect.state === 'accepted'
      && effect.request.taskId === task.id && effect.request.taskRevision === task.revision
      && (!effect.request.purpose || effect.request.purpose === 'execute'));
    // A known dispatch identifies the original workspace even if its partner
    // later changes Sessions. Never borrow another Session for a missing receipt.
    const sessionId = task.acceptedTurnId
      ? typeof producer?.request.sessionId === 'string' ? producer.request.sessionId : ''
      : participant?.sessionId ?? '';
    return task.artifacts.concat(task.evidence).flatMap(ref => {
      // Strip only recognized digest/line annotations from a local path. The
      // unmodified reference remains the evidence; opening reads the current file.
      const path = ref.trim().replace(/\s+(?:unchanged\s+)?sha256:[a-f\d]{64}$/iu, '')
        .replace(/#sha256:[a-f\d]{64}$/iu, '').replace(/:\d+(?::\d+)?$/u, '');
      if (/(?:\s|#)sha256:/iu.test(path)) return [];
      if (/[\r\n\0]/u.test(path) || (/^[a-z][a-z\d+.-]*:/iu.test(path) && !/^[a-z]:[\/\\]/iu.test(path))) return [];
      const match = path.match(/(?:^|[\/\\])([^\/\\?#:]+\.[a-z\d]{1,12})$/iu);
      const key = JSON.stringify([task.id, task.revision, ref]);
      if (!match || seen.has(key)) return [];
      seen.add(key);
      return [{ key, ref, path, name: match[1], task, sessionId, ownerName: participant ? roomPlanetName(participant.ordinal) : '' }];
    });
  });
}

/** Whether the rail has anything real to show; otherwise the conversation keeps the width. */
export function jevRailHasContent(graph: JevSnapshot | null): boolean {
  if (!graph) return false;
  return Boolean(jevLeafTasks(graph).length || graph.planApproval?.tasks.length || graph.rootAttachments?.length || graph.historicalTasks?.length
    || graph.effects.some(effect => effect.operation === 'dispatch' && ['prepared', 'admitted', 'running', 'unknown'].includes(effect.executionStatus)));
}

/**
 * One task rail beside the conversation: live partners, what waits and why,
 * accepted results and delivered files. Details open in a single drawer that
 * swaps its content in place, so there is never a dialog stacked on another.
 */
export function PawJevTeamPanels({ graph, room, projection, onOpenParticipant, onOpenFile, onCollapse, historical = false, active = true, observeCompletions = true, taskControls }: {
  graph: JevSnapshot | null; room?: RoomSummary; projection?: RoomProjectionState;
  onOpenParticipant: (id: string) => void; onOpenFile?: (sessionId: string, path: string) => void;
  /** Hide the rail; the header keeps a toggle to restore it. */
  onCollapse?: () => void;
  /** The graph is an explicitly selected earlier run, not the current task. */
  historical?: boolean;
  active?: boolean; observeCompletions?: boolean;
  taskControls?: JevTaskControls;
}) {
  const recentCompletions = useRecentTaskCompletions(graph, active && observeCompletions && !historical && !graph?.stopped);
  const [inspector, setInspector] = useState<Inspector | null>(null);
  const [collaborationVisited, setCollaborationVisited] = useState(false);
  const [deliveryVisited, setDeliveryVisited] = useState(false);
  const [deliveryState, setDeliveryState] = useState<DeliveryDeskState>(() => emptyDeliveryDesk(graph?.graphId ?? ''));
  const deliveryScope = graph?.graphId ?? '';
  const deliveryView = deliveryState.scope === deliveryScope ? deliveryState : emptyDeliveryDesk(deliveryScope);
  const changeDeliveryView = (change: Partial<DeliveryDeskState>) => setDeliveryState(previous => ({
    ...(previous.scope === deliveryScope ? previous : emptyDeliveryDesk(deliveryScope)), ...change, scope: deliveryScope,
  }));
  useEffect(() => { if (!inspector) { setCollaborationVisited(false); setDeliveryVisited(false); } }, [inspector]);
  const openCollaboration = () => { setDeliveryVisited(false); setCollaborationVisited(true); setInspector({ kind: 'collaboration' }); };
  const openDeliveries = () => { setCollaborationVisited(false); setDeliveryVisited(true); setInspector({ kind: 'deliveries' }); };
  const [expandedPartners, setExpandedPartners] = useState(false);
  const [railView, setRailView] = useState<'tasks' | 'files'>('tasks');
  const [fileQuery, setFileQuery] = useState('');
  const [taskFilter, setTaskFilter] = useState<JevRailFilter>('all');
  const [historyExpanded, setHistoryExpanded] = useState(false);
  const [openableOnly, setOpenableOnly] = useState(false);
  const motionAllowed = usePresentationMotion(active && !historical);
  const [collaborationTab, setCollaborationTab] = useState<'timeline' | 'map'>('timeline');
  const collaborationId = useId();
  const railTimeline = useRoomTimeline({ room, projection, graph, active: active && !historical, ...(graph?.rootId ? { rootId: graph.rootId } : {}) });
  const tabsRef = useRef<HTMLDivElement>(null);
  useEffect(() => { setRailView('tasks'); setFileQuery(''); setTaskFilter('all'); setHistoryExpanded(false); setOpenableOnly(false); }, [graph?.graphId]);
  const inspectorContent = useRef<HTMLDivElement>(null);
  const inspectorKey = inspector?.kind === 'task' ? `task:${inspector.task.id}`
    : inspector?.kind === 'plan' ? `plan:${inspector.task.key}`
    : inspector?.kind === 'panel' ? `panel:${inspector.panel}` : inspector?.kind === 'collaboration' ? 'collaboration' : inspector?.kind === 'deliveries' ? 'deliveries' : '';
  useEffect(() => {
    const content = inspectorContent.current;
    if (!content || !inspectorKey) return;
    content.scrollTop = 0;
    content.querySelectorAll<HTMLElement>('.paw-jev-inspector__details, .paw-jev-inspector__list').forEach(node => { node.scrollTop = 0; });
    // Returning to the explorer keeps the selected-task action in focus.
    // The original inspector remains the only dialog owner.
    const explorerAction = inspectorKey === 'collaboration'
      ? content.querySelector<HTMLElement>('.jcv-detail .jcv-button--primary')
      : inspectorKey === 'deliveries' ? content.querySelector<HTMLElement>('.jdd-detail h3')
        ?? content.querySelector<HTMLElement>('.jdd-search input') : null;
    (explorerAction ?? content).focus({ preventScroll: true });
  }, [inspectorKey]);
  const panelId = useId();
  const partners = jevPartnerWork(graph, room);
  const { tasks: mission, counts } = jevMission(graph);
  const tasks = mission.map(item => item.task);
  const files = fileEvidence(tasks, graph, room);
  const freshFiles = useReceiptHighlight(graph?.graphId ?? '', files.map(file => file.key), active && observeCompletions && !historical && !graph?.stopped);
  const query = fileQuery.trim().toLocaleLowerCase();
  const visibleFiles = files.filter(file => (!query || [file.name, file.path, file.ownerName].join(' ').toLocaleLowerCase().includes(query))
    && (!openableOnly || Boolean(onOpenFile && file.sessionId)));
  const openableCount = files.filter(file => onOpenFile && file.sessionId).length;
  const openFile = (file: (typeof files)[number]) => {
    if (onOpenFile && file.sessionId) { setInspector(null); onOpenFile(file.sessionId, file.path); }
    else setInspector({ kind: 'task', task: file.task });
  };
  const executingCount = tasks.filter(task => !['done', 'failed', 'cancelled'].includes(task.state) && graph?.effects.some(effect => effect.operation === 'dispatch' && effect.request.taskId === task.id && effect.request.taskRevision === task.revision && effect.executionStatus === 'running')).length;
  const history = mission.filter(item => item.lane === 'ended' && item.stage !== 'superseded');
  const pending = mission.filter(item => item.lane !== 'ended' && !partners.some(work => work.effects.some(effect => effect.request.taskId === item.task.id)));
  const assignments = graph?.planApproval && ['awaiting_approval', 'awaiting_input', 'deferred'].includes(graph.planApproval.status) ? graph.planApproval.tasks : [];
  const taskCount = tasks.length || assignments.length;
  const railMission = mission.filter(item => matchesJevRailFilter(item, taskFilter));
  const shownIds = new Set(railMission.map(item => item.task.id));
  const railPartners = taskFilter === 'all' ? partners : partners.filter(work => work.effects.some(effect => shownIds.has(String(effect.request.taskId))));
  const railPending = pending.filter(item => shownIds.has(item.task.id));
  const railHistory = history.filter(item => shownIds.has(item.task.id));
  const historyIssues = railHistory.filter(item => matchesJevRailFilter(item, 'attention'));
  const quietHistory = railHistory.filter(item => !matchesJevRailFilter(item, 'attention'));
  const planningEffect = graph?.effects.find(effect => effect.operation === 'dispatch' && effect.request.purpose === 'plan'
    && graph.tasks.some(task => task.id === effect.request.taskId && task.revision === effect.request.taskRevision));
  const planningActive = active && !graph?.stopped && !graph?.final && graph?.phase === 'plan' && planningEffect?.executionStatus === 'running';
  const planningLabel = !active ? '方案进度待同步' : graph?.stopped ? '规划已停止' : graph?.final ? '未形成执行任务'
    : graph?.phase === 'awaiting_input' ? '等待补充需求' : graph?.phase === 'awaiting_approval' ? '方案待确认'
    : graph?.phase === 'deferred' ? '方案暂未执行' : planningActive ? '正在拆分任务'
    : planningEffect?.executionStatus === 'unknown' ? '规划回执待核实' : '等待规划进度';
  const openParticipant = (id: string) => { setInspector(null); onOpenParticipant(id); };
  // An inspector must never keep showing a task from a previous graph.
  useEffect(() => setInspector(null), [graph?.graphId]);
  const ownerOf = (id: string) => room?.participants.find(item => item.id === id);
  const waitingText = (item: JevMissionTask) => item.waitingOn.length
    ? `等待 ${item.waitingOn.map(source => graph ? taskTitle(source, graph) : source.objective).slice(0, 2).join('、')}${item.waitingOn.length > 2 ? ` 等 ${item.waitingOn.length} 项` : ''} 验收`
    : item.reasons[0] ?? '';

  const toggleCards = (controls: string) => <button className="paw-jev-team__toggle" type="button" aria-expanded={expandedPartners} aria-controls={controls} aria-label={expandedPartners ? '收起伙伴卡片' : '展开伙伴卡片'} onClick={() => setExpandedPartners(value => !value)}>{expandedPartners ? '精简' : '详细'}<ChevronDown size={13} aria-hidden /></button>;
  const sectionHead = (kind: PanelKind, count: number, extra?: ReactNode) => <div className="paw-jev-rail__section-head">
    <button type="button" aria-label={`展开${kind === 'current' ? '当前伙伴' : PANEL_TITLES[kind]}`} onClick={() => setInspector({ kind: 'panel', panel: kind })}>
      <h3>{PANEL_TITLES[kind]}</h3><span>{count}</span><ChevronRight size={13} aria-hidden />
    </button>{extra}
  </div>;
  const renderCurrent = (filtered = false) => (filtered ? railPartners : partners).length ? <AnimatePresence initial={false}>{(filtered ? railPartners : partners).map(work => <PartnerWindow
    key={work.participant.id} work={work} graph={graph!} active={active && !historical} compact={!expandedPartners} projection={projection ? jevPartnerProjection(projection, graph!.rootId, work) : undefined}
    onOpenParticipant={openParticipant} onInspect={task => setInspector({ kind: 'task', task })} />)}</AnimatePresence> : null;
  const renderPending = (filtered = false) => (filtered ? railPending : pending).length ? <ul className="paw-jev-rail__list">{(filtered ? railPending : pending).map(item => {
    const owner = ownerOf(item.task.ownerId);
    return <li key={item.task.id}><button className="paw-jev-record-row" data-tone={item.tone} onClick={() => setInspector({ kind: 'task', task: item.task })} type="button">
      <JevActivityIcon state={item.stage} active={false} size={15} />
      <span><strong>{graph ? taskTitle(item.task, graph) : excerpt(item.task.objective, 36)}</strong>
        <small>{JEV_TASK_STAGE_LABELS[item.stage]}{owner ? ` · ${roomPlanetName(owner.ordinal)}` : ''}</small>
        {waitingText(item) ? <small className="paw-jev-rail__why"><GitBranch size={11} aria-hidden />{waitingText(item)}</small> : null}</span>
      <ChevronRight size={14} aria-hidden />
    </button></li>;
  })}</ul> : null;
  const renderHistory = (expanded: boolean, entries = history) => entries.length ? <ul className="paw-jev-rail__list">{(expanded ? entries : entries.slice(0, 4)).map(({ task, tone }) => {
    const owner = ownerOf(task.ownerId);
    const activity = !active || historical ? 'static' : task.state === 'failed' ? 'error' : task.state === 'cancelled' ? 'stopped' : recentCompletions.has(task.id) ? 'done' : 'static';
    return <li key={task.id}><button className="paw-jev-record-row" onClick={() => setInspector({ kind: 'task', task })} type="button" data-state={task.state} data-tone={tone}>
      {owner ? <RoomPlanetAvatar ordinal={owner.ordinal} size={24} activity={activity} decorative /> : <FolderOpen size={18} aria-hidden />}
      <span><strong>{owner ? roomPlanetName(owner.ordinal) : '任务记录'}</strong><span>{graph ? taskTitle(task, graph) : excerpt(task.objective, 36)}</span>
        <small>{task.state === 'done' ? <Check size={12} aria-hidden /> : <CircleAlert size={12} aria-hidden />}{TASK_STATES[task.state]}{task.artifacts.length ? ` · ${task.artifacts.length} 项产物` : ''}</small></span><ChevronRight size={14} aria-hidden />
    </button></li>;
  })}</ul> : null;
  const renderFiles = (expanded: boolean, filtered = false) => <>
    {graph?.rootAttachments?.length ? <><h4>原始附件 {graph.rootAttachments.length} 项</h4><ul className="paw-jev-input-files">{graph.rootAttachments.map(file => <li key={file.mediaId} title={file.fileName}><Paperclip size={14} aria-hidden /><span>{file.fileName}</span></li>)}</ul></> : null}
    {files.length ? <><h4>交付文件 {files.length} 项</h4><ul className="paw-jev-files">{(filtered ? visibleFiles : expanded ? files : files.slice(0, 5)).map(file => <li key={file.key} data-fresh={freshFiles.has(file.key) || undefined}>
      <button className="paw-jev-record-row" type="button" onClick={() => openFile(file)} title={file.ref}
        aria-label={onOpenFile && file.sessionId ? `打开文件 ${file.name}` : undefined}>
        <span className="paw-jev-file-mark" aria-hidden data-kind={fileKind(file.name)}><FileText size={18} /><small>{fileSuffix(file.name)}</small></span><span><strong>{file.name}</strong>{file.path !== file.name ? <span className="paw-jev-files__path">{file.path}</span> : null}<small>{file.ownerName ? `${file.ownerName} · ` : ''}{graph ? TASK_STATES[jevTaskStage(file.task, graph)] : '状态待同步'}{onOpenFile && file.sessionId ? '' : ' · 查看证据'}</small></span>
      </button>
      {onOpenFile && file.sessionId ? <button className="paw-jev-files__evidence" type="button" aria-label={`${file.name} 的任务与证据`} title="查看任务与原始证据" onClick={() => setInspector({ kind: 'task', task: file.task })}><ListChecks size={15} aria-hidden /></button> : null}
    </li>)}</ul>{!expanded && files.length > 5 ? <button className="paw-jev-records__more" type="button" onClick={() => setInspector({ kind: 'panel', panel: 'records' })}>查看全部 {files.length} 项交付文件<ChevronRight size={13} aria-hidden /></button> : null}</> : null}
  </>;
  const renderRecords = (expanded: boolean) => <section className="paw-jev-records">
    {renderHistory(expanded)}
    {!expanded && history.length > 4 ? <button className="paw-jev-records__more" type="button" onClick={() => setInspector({ kind: 'panel', panel: 'records' })}>查看全部 {history.length} 项任务<ChevronRight size={13} aria-hidden /></button> : null}
    {renderFiles(expanded)}
    {!history.length && !files.length && !graph?.rootAttachments?.length ? <p className="paw-jev-team__empty">尚无结束的任务。验收结果和交付文件会出现在这里。</p> : null}
    {graph?.historicalTasks?.length ? <details><summary>旧版本任务 · {graph.historicalTasks.length}</summary>
      <ul className="paw-jev-rail__list">{graph.historicalTasks.map(task => <li key={task.id}><button className="paw-jev-record-row" type="button" onClick={() => setInspector({ kind: 'task', task })}>
        <Layers2 size={15} aria-hidden /><span><strong>{taskTitle(task, graph)}</strong><small>已由新版本接手 · 保留原始记录</small></span><ChevronRight size={13} aria-hidden />
      </button></li>)}</ul></details> : null}
  </section>;
  const renderPlan = () => <section className="paw-jev-records">
    <ul className="paw-jev-rail__list">{assignments.map((task, index) => {
      const owner = ownerOf(task.ownerParticipantId);
      return <li key={task.key}><button className="paw-jev-record-row" onClick={() => setInspector({ kind: 'plan', task })} type="button">
        {owner ? <RoomPlanetAvatar ordinal={owner.ordinal} size={24} activity="static" decorative /> : <ListChecks size={18} aria-hidden />}
        <span><strong>{owner ? roomPlanetName(owner.ordinal) : '执行时分配'}</strong>
          <span>{graph ? taskTitle(task, graph) : excerpt(task.objective, 36)}</span>
          <small>待确认 · 未开始</small>
          <small className="paw-jev-rail__why">{task.dependsOn.length ? <><GitBranch size={11} aria-hidden />等待 {task.dependsOn.map(key => `任务 ${assignments.findIndex(item => item.key === key) + 1}`).join('、')}</> : `任务 ${index + 1} · 可立即开始`} · {task.acceptanceCriteria.length} 项验收</small></span>
        <ChevronRight size={14} aria-hidden />
      </button></li>;
    })}</ul>
  </section>;
  const renderPanel = (kind: PanelKind, expanded = false) => kind === 'current'
    ? renderCurrent() ?? <p className="paw-jev-team__empty">{graph?.final ? '本轮协作已结束。' : graph?.stopped ? '协作已停止，执行回执以最新同步为准。' : '收到伙伴执行回执后，在这里显示当前工作。'}</p>
    : kind === 'plan' ? renderPlan() : renderRecords(expanded);

  const inspectedTask = inspector?.kind === 'task' ? graph?.tasks.find(task => task.id === inspector.task.id) ?? graph?.historicalTasks?.find(task => task.id === inspector.task.id) ?? inspector.task : undefined;
  const inspectedFiles = inspectedTask ? fileEvidence([inspectedTask], graph, room) : [];
  const currentVersion = inspectedTask && graph ? jevCurrentTaskVersion(graph, inspectedTask.id) : undefined;
  const inspectedPlan = inspector?.kind === 'plan' ? assignments.find(task => task.key === inspector.task.key) ?? inspector.task : undefined;
  const owner = ownerOf(inspectedTask?.ownerId ?? inspectedPlan?.ownerParticipantId ?? '');
  const title = inspector?.kind === 'deliveries' ? '成果桌' : inspector?.kind === 'collaboration' ? '协作全景' : inspector?.kind === 'panel' ? PANEL_TITLES[inspector.panel] : `${owner ? roomPlanetName(owner.ordinal) + ' · ' : ''}${inspectedPlan ? '拟执行任务' : '任务与交付'}`;
  const ended = Boolean(graph?.final || graph?.stopped);
  return <>
    <aside className="paw-jev-rail paw-jev-team" aria-label="任务与成果" data-motion={active && !historical ? 'active' : 'paused'} data-view={railView} data-reduce-motion={!motionAllowed || undefined} data-summary={!partners.length && ended || undefined}>
      <header className="paw-jev-rail__head">
        <div><span className="paw-jev-rail__eyebrow">{historical ? '保留的工作记录' : '本轮工作'}</span><strong>任务与成果</strong>{historical ? <small>历史记录 · 非当前任务</small> : null}</div>
        {onCollapse ? <button type="button" className="paw-jev-rail__collapse" aria-label="收起任务栏" title="收起任务栏" onClick={onCollapse}><PanelRightClose size={16} aria-hidden /></button> : null}
      </header>
      <div className="paw-jev-team__progress" aria-label="任务完成进度">
        {!graph ? <span>{active ? '正在同步任务进度' : '任务进度待同步'}</span>
          : tasks.length ? <>
            <strong>已验收 {counts.accepted}/{taskCount}</strong>
            {executingCount > 0 ? <span>{active ? '执行中' : '上次执行中'} {executingCount}</span> : null}
            {counts.reviewing > 0 ? <span>待复核 {counts.reviewing}</span> : null}
            {counts.waiting ? <span>等待 {counts.waiting}</span> : null}
          </> : <span>{planningActive ? <LoaderCircle className="paw-jev-partner__spinner" size={12} aria-hidden /> : null}{planningLabel}</span>}
      </div>
      {railTimeline && railTimeline.lanes.some(lane => lane.kind === 'partner' && railTimeline.segments.some(segment => segment.laneId === lane.id))
        ? <CollabTimelinePeek timeline={railTimeline} active={motionAllowed} renderAvatar={roomPlanetAvatarRenderer(false)} onExpand={() => { setCollaborationTab('timeline'); openCollaboration(); }} />
        : <JevCollaborationPanel graph={graph} room={room} projection={projection} active={active} historical={historical}
          observeCompletions={observeCompletions} presentation="peek" onExpand={() => { setCollaborationTab('map'); openCollaboration(); }}
          onInspectTask={node => { if (node.task) setInspector({ kind: 'task', task: node.task }); else if (node.proposal) setInspector({ kind: 'plan', task: node.proposal }); }} />}
      <div className="paw-jev-rail__tabs" role="tablist" aria-label="任务栏内容" ref={tabsRef}
        onKeyDown={event => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
          event.preventDefault();
          const next = event.key === 'Home' ? 'tasks' : event.key === 'End' ? 'files' : railView === 'tasks' ? 'files' : 'tasks';
          setRailView(next);
          tabsRef.current?.querySelector<HTMLButtonElement>(`[data-tab="${next}"]`)?.focus();
        }}>
        <button type="button" role="tab" id={`${panelId}-tasks-tab`} data-tab="tasks" aria-selected={railView === 'tasks'} aria-controls={`${panelId}-tasks-panel`} tabIndex={railView === 'tasks' ? 0 : -1} onClick={() => setRailView('tasks')}><ListChecks size={15} aria-hidden />任务<span>{taskCount}</span></button>
        <button type="button" role="tab" id={`${panelId}-files-tab`} data-tab="files" aria-selected={railView === 'files'} aria-controls={`${panelId}-files-panel`} tabIndex={railView === 'files' ? 0 : -1} onClick={() => setRailView('files')}><FolderOpen size={15} aria-hidden />成果<span>{files.length}</span>{freshFiles.size ? <i aria-label="有新交付文件" /> : null}</button>
      </div>
      {railView === 'tasks' && mission.length ? <JevRailFilters items={mission} value={taskFilter} onChange={setTaskFilter} /> : null}
      <div className="paw-jev-team__content" role="tabpanel" id={`${panelId}-tasks-panel`} aria-labelledby={`${panelId}-tasks-tab`} hidden={railView !== 'tasks'} tabIndex={0}>
        {!active && partners.length ? <p className="paw-jev-team__sync">显示上次同步，动态已暂停</p> : null}
        {assignments.length ? <section className="paw-jev-rail__section">{sectionHead('plan', assignments.length)}<p className="paw-jev-rail__note">确认完整方案后，再按依赖开始。</p>{renderPlan()}</section> : null}
        {railPartners.length ? <section className="paw-jev-rail__section" id={`${panelId}-current`}>{sectionHead('current', railPartners.length, toggleCards(`${panelId}-current`))}{renderCurrent(true)}</section> : null}
        {railPending.length ? <section className="paw-jev-rail__section"><div className="paw-jev-rail__section-head"><h3>等待与待处理</h3><span>{railPending.length}</span></div>{renderPending(true)}</section> : null}
        {historyIssues.length ? <section className="paw-jev-rail__section paw-jev-rail__attention"><div className="paw-jev-rail__section-head"><h3><CircleAlert size={14} aria-hidden />需要关注</h3><span>{historyIssues.length}</span></div>{renderHistory(true, historyIssues)}</section> : null}
        {quietHistory.length ? <section className="paw-jev-rail__section"><details className="paw-jev-ended-group"
          open={historyExpanded || taskFilter === 'ended' || taskFilter === 'attention'}
          onToggle={event => { if (taskFilter !== 'ended' && taskFilter !== 'attention') setHistoryExpanded(event.currentTarget.open); }}>
          <summary><Check size={14} aria-hidden /><span>已结束</span><small>{quietHistory.length}</small><ChevronDown size={13} aria-hidden /></summary>
          {renderHistory(true, quietHistory)}</details></section> : null}
        {taskFilter !== 'all' && !railMission.length ? <div className="paw-jev-rail__empty"><ListChecks size={25} aria-hidden /><strong>这类任务暂无记录</strong><p>筛选只影响列表，不改变任务运行。</p><button type="button" onClick={() => setTaskFilter('all')}>显示全部任务</button></div> : null}
        {!assignments.length && !partners.length && !pending.length && !history.length ? <div className="paw-jev-rail__empty"><ListChecks size={28} aria-hidden /><strong>等待任务回执</strong><p>分工和结果会出现在这里。当前没有可展示的执行任务。</p></div> : null}
        {taskFilter === 'all' && graph?.historicalTasks?.length ? <section className="paw-jev-rail__section"><details><summary>旧版本任务 · {graph.historicalTasks.length}</summary><ul className="paw-jev-rail__list">{graph.historicalTasks.map(task => <li key={task.id}><button type="button" className="paw-jev-record-row" onClick={() => setInspector({ kind: 'task', task })}><Layers2 size={15} aria-hidden /><span><strong>{excerpt(task.objective, 60)}</strong><small>修订 {task.revision} · 查看历史依据</small></span><ChevronRight size={14} aria-hidden /></button></li>)}</ul></details></section> : null}
        {files.length ? <JevDeliveryDeskLaunch count={files.length} onClick={openDeliveries} /> : null}
      </div>
      <div className="paw-jev-team__content paw-jev-rail__files-panel" role="tabpanel" id={`${panelId}-files-panel`} aria-labelledby={`${panelId}-files-tab`} hidden={railView !== 'files'} tabIndex={0}>
        {files.length ? <JevDeliveryDeskLaunch count={files.length} onClick={openDeliveries} /> : null}
        {files.length ? <label className="paw-jev-file-search"><Search size={15} aria-hidden /><input type="search" aria-label="筛选交付文件" placeholder="查找文件或路径" value={fileQuery} onChange={event => setFileQuery(event.target.value)} />{fileQuery ? <button type="button" aria-label="清除文件筛选" onClick={() => setFileQuery('')}><X size={14} aria-hidden /></button> : null}</label> : null}
        {graph?.final ? <div className="paw-jev-delivery-summary" data-complete={graph.final.status === 'completed' || undefined}>{graph.final.status === 'completed' ? <Check size={18} aria-hidden /> : <CircleAlert size={18} aria-hidden />}<span><strong>{graph.final.status === 'completed' ? '结果已汇总' : '任务尚未完整交付'}</strong><small>最终答复保留在对话中，文件和依据保留在这里。</small></span></div> : null}
        {files.length ? <div className="paw-jev-file-scope"><span>{query || openableOnly ? `显示 ${visibleFiles.length}/${files.length} 项` : `${files.length} 项交付记录`}</span>
          {onOpenFile ? <button type="button" aria-pressed={openableOnly} onClick={() => setOpenableOnly(!openableOnly)} title="只显示能打开当前工作区文件的交付记录">可打开 {openableCount}</button> : null}</div> : null}
        {renderFiles(true, true)}
        {(query || openableOnly) && !visibleFiles.length ? <p className="paw-jev-rail__no-match" role="status">没有匹配的交付文件。<button type="button" onClick={() => { setFileQuery(''); setOpenableOnly(false); }}>清除筛选</button></p> : null}
        {!files.length ? <div className="paw-jev-rail__empty"><FolderOpen size={30} aria-hidden /><strong>还没有交付文件</strong><p>伙伴实际提交文件后会出现在这里，计划中的文件不会提前显示。</p><button type="button" onClick={() => setRailView('tasks')}>查看任务进展<ChevronRight size={14} aria-hidden /></button></div> : null}
      </div>
    </aside>
    <Dialog open={Boolean(inspector)} onOpenChange={open => { if (!open) setInspector(null); }}><DialogContent ref={inspectorContent} tabIndex={-1} className="paw-jev-inspector" data-delivery-desk={inspector?.kind === 'deliveries' || undefined} data-collaboration={inspector?.kind === 'collaboration' || undefined} data-reduce-motion={!motionAllowed || undefined} data-motion={active ? 'active' : 'paused'} onOpenAutoFocus={event => {
        event.preventDefault();
        inspectorContent.current?.focus({ preventScroll: true });
      }}>
      <DialogHeader className="paw-jev-inspector__header"><span className="paw-jev-inspector__eyebrow">任务板 / {inspector?.kind === 'deliveries' ? '成果桌' : inspector?.kind === 'collaboration' ? '分工与依赖' : inspector?.kind === 'panel' ? '全部记录' : inspectedPlan ? '执行方案' : '任务详情'}</span><DialogTitle>{title}</DialogTitle><DialogDescription>{inspector?.kind === 'deliveries' ? '浏览文件记录，整理本窗口重点，回到原任务核对结果。' : inspector?.kind === 'collaboration' ? '查看当前任务、依赖关系、执行者与复核者。不会改变任务调度。' : inspector?.kind === 'panel' ? '来自当前方案、任务状态与执行回执。' : '完整责任、验收与交付依据；执行历史可在伙伴 Session 中查看。'}</DialogDescription></DialogHeader>
      {collaborationVisited && inspector ? <div className="paw-jev-collaboration-mount" hidden={inspector.kind !== 'collaboration'}>
        {railTimeline ? <div className="paw-jev-collaboration-switch" role="tablist" aria-label="协作全景视图"
          onKeyDown={event => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
            event.preventDefault();
            const next = event.key === 'Home' ? 'timeline' : event.key === 'End' ? 'map' : collaborationTab === 'timeline' ? 'map' : 'timeline';
            setCollaborationTab(next);
            document.getElementById(`${collaborationId}-${next}`)?.focus();
          }}>
          {(['timeline', 'map'] as const).map(tab => <button key={tab} id={`${collaborationId}-${tab}`} type="button" role="tab"
            aria-controls={`${collaborationId}-${tab}-panel`} tabIndex={collaborationTab === tab ? 0 : -1}
            aria-selected={collaborationTab === tab} onClick={() => setCollaborationTab(tab)}>{tab === 'timeline' ? '协作时间线' : '任务关系'}</button>)}
        </div> : null}
        {railTimeline ? <div className="paw-jev-collaboration-pane" id={`${collaborationId}-timeline-panel`} role="tabpanel"
          aria-labelledby={`${collaborationId}-timeline`} hidden={collaborationTab !== 'timeline'}>
          <CollabTimelineStage timeline={railTimeline} active={motionAllowed && inspector.kind === 'collaboration' && collaborationTab === 'timeline'} renderAvatar={roomPlanetAvatarRenderer(motionAllowed && collaborationTab === 'timeline')}
            onOpenLane={lane => { if (lane.kind === 'partner') openParticipant(lane.id); }} />
        </div> : null}
        <div className="paw-jev-collaboration-pane" id={`${collaborationId}-map-panel`} role={railTimeline ? 'tabpanel' : undefined}
          aria-labelledby={railTimeline ? `${collaborationId}-map` : undefined} hidden={Boolean(railTimeline) && collaborationTab !== 'map'}>
          <JevCollaborationPanel graph={graph} room={room} projection={projection} active={active && inspector.kind === 'collaboration' && (!railTimeline || collaborationTab === 'map')}
            historical={historical} observeCompletions={observeCompletions} onOpenParticipant={openParticipant}
            onInspectTask={node => { if (node.task) setInspector({ kind: 'task', task: node.task }); else if (node.proposal) setInspector({ kind: 'plan', task: node.proposal }); }} />
        </div>
      </div> : null}
      {deliveryVisited && inspector ? <div className="paw-jev-delivery-mount" hidden={inspector.kind !== 'deliveries'}>
        <JevDeliveryDesk files={files} state={deliveryView} onState={changeDeliveryView}
          active={inspector.kind === 'deliveries'} motion={motionAllowed && observeCompletions && !historical && !graph?.stopped && !graph?.final}
          historical={historical} paused={!active && !historical} freshKeys={freshFiles} attachments={graph?.rootAttachments}
          stageLabel={file => graph ? TASK_STATES[jevTaskStage(file.task, graph)] : '状态待同步'}
          renderOwner={file => { const participant = ownerOf(file.task.ownerId); return participant ? <RoomPlanetAvatar ordinal={participant.ordinal} size={28} decorative activity="static" /> : null; }}
          onInspect={file => setInspector({ kind: 'task', task: file.task })}
          {...(onOpenFile ? { onOpen: openFile } : {})} />
      </div> : null}
      {inspector?.kind === 'collaboration' || inspector?.kind === 'deliveries' ? null : inspector?.kind === 'panel' ? <>{inspector.panel === 'current' ? <div className="paw-jev-inspector__display">{toggleCards(`${panelId}-dialog-current`)}</div> : null}<div className="paw-jev-inspector__list" id={`${panelId}-dialog-current`}>{renderPanel(inspector.panel, true)}{inspector.panel === 'current' && pending.length ? <><h4>等待与待处理</h4>{renderPending()}</> : null}</div></> : <div className="paw-jev-inspector__details">
        <section className="paw-jev-inspector__objective"><h3>任务目标</h3><p>{inspectedTask?.objective ?? inspectedPlan?.objective}</p></section>
        {inspectedTask && !graph ? <p className="paw-jev-inspector__status">状态待同步</p> : null}
        {inspectedTask && graph ? <JevTaskProgress task={inspectedTask} graph={graph} active={active && !historical} /> : null}
        {inspectedTask && graph && taskControls && (!graph.activeTaskIds || graph.activeTaskIds.includes(inspectedTask.id)) ? <JevTaskAssignment key={`${graph.graphId}:${inspectedTask.id}`} graph={graph} task={inspectedTask} room={room} controls={taskControls} active={active} /> : null}
        {inspectedTask && graph && taskControls?.revision && (taskControls.revision.pending(graph.graphId, inspectedTask.id)
          || graph.activeTaskIds?.includes(inspectedTask.id) && !graph.final && !graph.stopped)
          ? <JevTaskRevision key={`revision:${graph.graphId}:${inspectedTask.id}`} graph={graph} task={inspectedTask} controls={taskControls.revision} active={active} /> : null}
        {currentVersion && currentVersion.id !== inspectedTask?.id ? <Button variant="quiet" onClick={() => setInspector({ kind: 'task', task: currentVersion })}>查看当前版本任务<ChevronRight size={14} aria-hidden /></Button> : null}
        {(inspectedTask?.expectedOutput || inspectedPlan?.expectedOutput) ? <section><h3>预期产出</h3><p>{inspectedTask?.expectedOutput || inspectedPlan?.expectedOutput}</p></section> : null}
        {(inspectedTask?.acceptance.length || inspectedPlan?.acceptanceCriteria.length) ? <section className="paw-jev-inspector__criteria"><h3>验收要求</h3><ul>{(inspectedTask?.acceptance ?? inspectedPlan?.acceptanceCriteria ?? []).map((item, index) => <li key={index}>{item}</li>)}</ul></section> : null}
        {inspectedPlan?.writeTargets.length ? <section><h3>写入范围</h3><ul>{inspectedPlan.writeTargets.map(item => <li key={item}>{item}</li>)}</ul></section> : null}
        {inspectedTask && graph?.edges.some(edge => edge.dependent === inspectedTask.id) ? <section><h3>任务交接来源</h3><ul>{graph.edges.filter(edge => edge.dependent === inspectedTask.id).map(edge => {
          const source = graph.tasks.find(task => task.id === edge.prerequisite);
          return <li key={edge.prerequisite}>{source ? <button className="paw-jev-inspector__dependency" type="button" onClick={() => setInspector({ kind: 'task', task: source })}><JevActivityIcon state={jevTaskStage(source, graph)} active={active} size={15} /><span>{edge.kind === 'context' ? '参考' : '依赖'}：{taskTitle(source, graph)}<small>{TASK_STATES[jevTaskStage(source, graph)]}</small></span><ChevronRight size={14} aria-hidden /></button> : `${edge.prerequisite} · 待同步`}</li>;
        })}</ul></section> : null}
        {inspectedPlan?.dependsOn.length ? <section><h3>前置任务</h3><ul>{inspectedPlan.dependsOn.map(key => { const source = assignments.find(task => task.key === key); return <li key={key}>{source ? <button className="paw-jev-inspector__dependency" type="button" onClick={() => setInspector({ kind: 'plan', task: source })}><span>{excerpt(source.objective, 60)}</span><ChevronRight size={14} aria-hidden /></button> : key}</li>; })}</ul></section> : null}
        {inspectedTask?.result ? <section className="paw-jev-inspector__result"><h3>结果记录</h3><p>{inspectedTask.result}</p></section> : null}
        {inspectedTask && inspectedTask.artifacts.concat(inspectedTask.evidence).length ? <section><h3>产物与证据</h3>
          {onOpenFile && inspectedFiles.some(file => file.sessionId) ? <p className="paw-jev-inspector__file-note">打开伙伴工作区中的当前文件，交付时的原始证据保留在下方。</p> : null}
          <ul className="paw-jev-inspector__refs">{[...new Set(inspectedTask.artifacts.concat(inspectedTask.evidence))].map(ref => {
            const file = inspectedFiles.find(item => item.ref === ref);
            return <li key={ref}><code>{ref}</code>{file?.sessionId && onOpenFile ? <button className="paw-jev-inspector__open-file" type="button" onClick={() => openFile(file)} aria-label={`打开当前文件 ${file.name}`}><FileText size={13} aria-hidden />打开文件</button> : null}</li>;
          })}</ul></section> : null}
        <footer><Button variant="quiet" onClick={() => deliveryVisited ? setInspector({ kind: 'deliveries' }) : collaborationVisited ? setInspector({ kind: 'collaboration' }) : setInspector({ kind: 'panel', panel: inspectedPlan ? 'plan' : partners.some(work => work.effects.some(effect => effect.request.taskId === inspectedTask?.id)) ? 'current' : 'records' })} leadingIcon={<ArrowLeft size={14} />}>{deliveryVisited ? '返回成果桌' : collaborationVisited ? '返回协作全景' : '返回列表'}</Button>{owner ? <Button onClick={() => openParticipant(owner.id)} leadingIcon={<ExternalLink size={14} />}>打开 {roomPlanetName(owner.ordinal)} Session</Button> : null}</footer>
      </div>}
    </DialogContent></Dialog>
  </>;
}

/** Revision-qualified completion keys distinguish a new accepted revision from old history. */
function useRecentTaskCompletions(graph: JevSnapshot | null, observing: boolean): ReadonlySet<string> {
  const fresh = useReceiptHighlight(graph?.graphId ?? '', graph?.tasks.filter(task => task.state === 'done')
    .map(task => `${task.id}:${task.revision}`) ?? [], observing, 1000);
  return new Set(graph?.tasks.filter(task => fresh.has(`${task.id}:${task.revision}`)).map(task => task.id) ?? []);
}

function PartnerWindow({ work, graph, projection, active, compact, onOpenParticipant, onInspect }: {
  work: JevPartnerWork; graph: JevSnapshot; projection?: RoomProjectionState; active: boolean; compact: boolean;
  onOpenParticipant: (id: string) => void; onInspect: (task: JevTask) => void;
}) {
  const current = work.effects[0];
  const systemReduced = useReducedMotion();
  const motionAllowed = usePresentationMotion(active);
  const reduced = systemReduced || !motionAllowed;
  const present = useIsPresent();
  const purpose = String(current.request.purpose || 'execute');
  const running = current.executionStatus === 'running';
  const name = roomPlanetName(work.participant.ordinal);
  const tasks = work.effects.flatMap(effect => graph.tasks.filter(task => task.id === effect.request.taskId));
  const latest = projection && selectRoomParticipantPublicProgress(projection).find(item => ['tool', 'progress', 'post'].includes(item.kind));
  const activity = !active || !present ? 'static' : graph.stopped ? 'stopped' : running ? purpose === 'plan' || purpose === 'synthesize' ? 'thinking' : 'working' : current.executionStatus === 'unknown' ? 'static' : 'waiting';
  const model = executionModel(current);
  const spinning = running && active && present && !graph.stopped;
  const firstTask = tasks[0];
  const outcome = latest?.kind === 'tool' ? toolExecutionOutcome(latest.data ?? {}) : undefined;
  const latestState = outcome === 'unknown' ? '回执待核实' : outcome === 'not_started' ? '尚未执行' : outcome === 'applied' ? '已完成' : latest ? { running: '执行中', waiting: '等待中', completed: '已完成', failed: '失败', aborted: '已停止' }[latest.status] : '';
  return <motion.section className="paw-jev-partner" aria-label={`${name} 当前工作`} data-state={current.executionStatus} data-purpose={purpose} data-compact={compact || undefined} data-exiting={!present || undefined} data-motion={spinning ? 'active' : 'paused'}
    initial={{ opacity: 0, y: reduced || !active ? 0 : 5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: reduced || !active ? 0 : -3 }} transition={{ duration: reduced ? 0 : .18, ease: [.23, 1, .32, 1] }}>
    <header><RoomPlanetAvatar ordinal={work.participant.ordinal} size={32} activity={activity} decorative /><div><strong>{name}</strong>{model ? <span title={model}>{model}</span> : null}</div><button type="button" aria-label={`打开 ${name} Session`} onClick={() => onOpenParticipant(work.participant.id)}><ExternalLink size={14} aria-hidden /></button></header>
    {firstTask ? <button className="paw-jev-partner__task-title" type="button" onClick={() => onInspect(firstTask)}><span>{taskTitle(firstTask, graph)}</span>{tasks.length > 1 ? <small> +{tasks.length - 1}</small> : null}<ChevronRight size={13} aria-hidden /></button> : null}
    <p className="paw-jev-partner__state"><JevActivityIcon size={15} state={graph.stopped ? 'reclaiming' : current.executionStatus === 'unknown' ? 'unknown' : running ? purpose === 'plan' ? 'planning' : purpose === 'verify' ? 'verifying' : purpose === 'synthesize' ? 'synthesizing' : 'running' : current.state === 'sending' ? 'dispatching' : 'queued'} active={spinning || active && present && current.state === 'sending' && !graph.stopped} />
      <span>{PURPOSES[purpose] || '当前任务'} · {!active ? '上次同步 · ' : ''}{graph.stopped ? '正在停止，等待回执' : STATUS[current.executionStatus] || '状态待核实'}</span>
    </p>
    {firstTask && !compact ? <JevTaskProgress task={firstTask} graph={graph} active={active && present} compact /> : null}
    <button className="paw-jev-partner__recent" type="button" aria-label={`查看 ${name} 完整过程`} onClick={() => onOpenParticipant(work.participant.id)}>
      <span>{compact ? latestState : `最近动作${latestState ? ` · ${latestState}` : ''}`}</span>
      <strong>{latest ? excerpt(latest.summary, 64) : projection ? '等待伙伴公开过程' : '正在同步公开过程'}</strong>
    </button>
  </motion.section>;
}


/** Stage receipts, not an estimate of elapsed work or time remaining. */
export function JevTaskProgress({ task, graph, active, compact = false }: {
  task: JevTask; graph: JevSnapshot; active: boolean; compact?: boolean;
}) {
  const projectedStage = jevTaskStage(task, graph);
  const stage = projectedStage === 'review' && graph.effects.some(effect => effect.operation === 'dispatch'
    && effect.request.taskId === task.id && effect.request.taskRevision === task.revision
    && effect.request.purpose === 'verify' && effect.executionStatus === 'running') ? 'verifying' : projectedStage;
  const current = stage === 'done' ? 4 : stage === 'review' || stage === 'verifying' ? 2
    : stage === 'submitted' ? 1 : ['running', 'dispatched', 'dispatching'].includes(stage) ? 0 : -1;
  const moving = active && !graph.stopped && !graph.final && ['running', 'verifying'].includes(stage);
  return <section className="paw-jev-task-progress" data-compact={compact || undefined} aria-label="任务阶段进度">
    {!compact ? <header><strong>任务阶段进度</strong><small>按实际回执更新 · 非耗时百分比</small></header> : null}
    <ol>{['执行', '提交', '复核', '验收'].map((label, index) => {
      const complete = current > index;
      const selected = current === index;
      return <li key={label} data-complete={complete || undefined} aria-current={selected ? 'step' : undefined}>
        {complete ? <Check size={13} aria-hidden /> : selected && moving ? <LoaderCircle className="paw-jev-partner__spinner" size={13} aria-hidden /> : <Circle size={13} aria-hidden />}
        <span>{label}</span>
      </li>;
    })}</ol>
    {!compact ? <p>{!active ? '显示上次同步状态 · ' : ''}{graph.stopped && stage !== 'done' ? '协作已停止' : TASK_STATES[stage]}{stage === 'submitted' ? ' · 等待结构化结果回执' : ''}</p> : null}
  </section>;
}

/** File type decorates the receipt; it never invents a viewer or download. */
function fileSuffix(name: string): string {
  return name.includes('.') ? name.split('.').at(-1)!.slice(0, 5).toUpperCase() : 'FILE';
}
function fileKind(name: string): string {
  const ext = fileSuffix(name);
  if (['TS', 'TSX', 'JS', 'JSX', 'PY', 'CSS', 'HTML', 'SH'].includes(ext)) return 'code';
  if (['PNG', 'JPG', 'JPEG', 'SVG', 'WEBP'].includes(ext)) return 'image';
  if (['JSON', 'CSV', 'YAML', 'YML', 'XLSX'].includes(ext)) return 'data';
  return 'document';
}
