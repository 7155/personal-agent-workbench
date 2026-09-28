import { ArrowLeft, Check, Circle, ChevronDown, ChevronRight, CircleAlert, ExternalLink, FileText, FolderOpen, ListChecks, LoaderCircle, UsersRound, Workflow } from 'lucide-react';
import { AnimatePresence, motion, useIsPresent, useReducedMotion } from 'motion/react';
import { useEffect, useId, useRef, useState } from 'react';
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
import { JEV_TASK_STAGE_LABELS as TASK_STATES, jevCurrentTaskVersion, jevLeafTasks, jevTaskStage, type JevEffect, type JevPlanTask, type JevSnapshot, type JevTask } from '@/features/semantic-workspace/jev-execution';
import './paw-jev-team.css';

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

type PanelKind = 'current' | 'records' | 'plan' | 'criteria';
type Inspector = { kind: 'panel'; panel: PanelKind } | { kind: 'task'; task: JevTask } | { kind: 'plan'; task: JevPlanTask };
const PANEL_TITLES: Record<PanelKind, string> = { current: '当前伙伴', records: '阶段与交付记录', plan: '拟执行分工', criteria: '范围与验收' };

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
      const key = `${task.id}:${ref}`;
      if (!match || seen.has(key)) return [];
      seen.add(key);
      return [{ key, ref, path, name: match[1], task, sessionId, ownerName: participant ? roomPlanetName(participant.ordinal) : '' }];
    });
  });
}

export function PawJevTeamPanels({ graph, room, projection, onOpenParticipant, onOpenFile, active = true, observeCompletions = true, taskControls }: {
  graph: JevSnapshot | null; room?: RoomSummary; projection?: RoomProjectionState;
  onOpenParticipant: (id: string) => void; onOpenFile?: (sessionId: string, path: string) => void;
  active?: boolean; observeCompletions?: boolean;
  taskControls?: JevTaskControls;
}) {
  const recentCompletions = useRecentTaskCompletions(graph, active && observeCompletions && !graph?.stopped);
  const [inspector, setInspector] = useState<Inspector | null>(null);
  const [expandedPartners, setExpandedPartners] = useState(false);
  const inspectorContent = useRef<HTMLDivElement>(null);
  const inspectorKey = inspector?.kind === 'task' ? `task:${inspector.task.id}`
    : inspector?.kind === 'plan' ? `plan:${inspector.task.key}`
    : inspector?.kind === 'panel' ? `panel:${inspector.panel}` : '';
  useEffect(() => {
    const content = inspectorContent.current;
    if (!content || !inspectorKey) return;
    content.scrollTop = 0;
    content.focus({ preventScroll: true });
  }, [inspectorKey]);
  const panelId = useId();
  const partners = jevPartnerWork(graph, room);
  const tasks = jevLeafTasks(graph);
  const files = fileEvidence(tasks, graph, room);
  const openFile = (file: (typeof files)[number]) => {
    if (onOpenFile && file.sessionId) { setInspector(null); onOpenFile(file.sessionId, file.path); }
    else setInspector({ kind: 'task', task: file.task });
  };
  const acceptedCount = tasks.filter(task => graph && jevTaskStage(task, graph) === 'done').length;
  const executingCount = tasks.filter(task => !['done', 'failed', 'cancelled'].includes(task.state) && graph?.effects.some(effect => effect.operation === 'dispatch' && effect.request.taskId === task.id && effect.request.taskRevision === task.revision && effect.executionStatus === 'running')).length;
  const reviewCount = tasks.filter(task => graph && jevTaskStage(task, graph) === 'review').length;
  const history = tasks.filter(task => graph && ['done', 'failed', 'cancelled'].includes(jevTaskStage(task, graph)));
  const pending = tasks.filter(task => graph && !['done', 'failed', 'cancelled'].includes(jevTaskStage(task, graph)) && !partners.some(work => work.effects.some(effect => effect.request.taskId === task.id)));
  const assignments = graph?.planApproval && ['awaiting_approval', 'awaiting_input', 'deferred'].includes(graph.planApproval.status) ? graph.planApproval.tasks : [];
  const taskCount = tasks.length || assignments.length;
  const planningEffect = graph?.effects.find(effect => effect.operation === 'dispatch' && effect.request.purpose === 'plan'
    && graph.tasks.some(task => task.id === effect.request.taskId && task.revision === effect.request.taskRevision));
  const planningActive = active && !graph?.stopped && !graph?.final && graph?.phase === 'plan' && planningEffect?.executionStatus === 'running';
  const planningLabel = !active ? '方案进度待同步' : graph?.stopped ? '规划已停止' : graph?.final ? '未形成执行任务'
    : graph?.phase === 'awaiting_input' ? '等待补充需求' : graph?.phase === 'awaiting_approval' ? '方案待确认'
    : graph?.phase === 'deferred' ? '方案暂未执行' : planningActive ? '正在拆分任务'
    : planningEffect?.executionStatus === 'unknown' ? '规划回执待核实' : '等待规划进度';
  // Keep the current partner and delivery record in stable places as work
  // moves from execution to review. Swapping both rails loses reading position.
  const panels: PanelKind[] = assignments.length ? ['plan', 'criteria'] : ['current', 'records'];
  const count = (kind: PanelKind) => kind === 'current' ? partners.length : kind === 'records' ? history.length + pending.length : assignments.length;
  const openParticipant = (id: string) => { setInspector(null); onOpenParticipant(id); };
  // An inspector must never keep showing a task from a previous graph.
  useEffect(() => setInspector(null), [graph?.graphId]);

  const toggleCards = (controls: string) => <button className="paw-jev-team__toggle" type="button" aria-expanded={expandedPartners} aria-controls={controls} aria-label={expandedPartners ? '收起伙伴卡片' : '展开伙伴卡片'} onClick={() => setExpandedPartners(value => !value)}>{expandedPartners ? '收起' : '展开'}<ChevronDown size={13} aria-hidden /></button>;
  const renderPanel = (kind: PanelKind, expanded = false) => {
    if (kind === 'current') return partners.length ? <AnimatePresence initial={false}>{partners.map(work => <PartnerWindow
      key={work.participant.id} work={work} graph={graph!} active={active} compact={!expandedPartners} projection={projection ? jevPartnerProjection(projection, graph!.rootId, work) : undefined}
      onOpenParticipant={openParticipant} onInspect={task => setInspector({ kind: 'task', task })} />)}</AnimatePresence>
      : <p className="paw-jev-team__empty">{graph?.final ? '本轮协作已结束，可查看交付记录。' : graph?.stopped ? '协作已停止，执行回执以最新同步为准。' : '收到伙伴执行回执后，在这里显示当前工作。'}</p>;
    if (kind === 'plan' || kind === 'criteria') return <section className="paw-jev-records">
      <p>{kind === 'plan' ? '方案中的责任分工，确认后开始。' : '核对产出、范围与验收要求。'}</p>
      <ul>{assignments.map(task => {
        const owner = room?.participants.find(item => item.id === task.ownerParticipantId);
        return <li key={task.key}><button className="paw-jev-record-row" onClick={() => setInspector({ kind: 'plan', task })} type="button">
          {kind === 'plan' && owner ? <RoomPlanetAvatar ordinal={owner.ordinal} size={38} activity="static" decorative /> : <ListChecks size={22} aria-hidden />}
          <span><strong>{kind === 'plan' ? owner ? roomPlanetName(owner.ordinal) : '执行时分配' : graph ? taskTitle(task, graph) : excerpt(task.objective, 36)}</strong>
            <span>{kind === 'plan' ? graph ? taskTitle(task, graph) : excerpt(task.objective, 36) : `${task.acceptanceCriteria.length} 项验收要求${task.writeTargets.length ? ` · ${task.writeTargets.length} 项写入范围` : ''}`}</span>
            {kind === 'plan' ? <small>待确认 · 未开始</small> : null}</span><ChevronRight size={15} aria-hidden />
        </button></li>;
      })}</ul>
    </section>;
    return <section className="paw-jev-records">
      {graph?.rootAttachments?.length ? <><h3>原始附件</h3><ul className="paw-jev-input-files">{graph.rootAttachments.map(file => <li key={file.mediaId} title={file.fileName}><FileText size={15} aria-hidden /><span>{file.fileName}</span></li>)}</ul></> : null}
      {files.length ? <><h3>文档与文件证据</h3><ul className="paw-jev-files">{(expanded ? files : files.slice(0, 4)).map(file => <li key={file.key}>
        <button className="paw-jev-record-row" type="button" onClick={() => openFile(file)} title={file.ref}
          aria-label={onOpenFile && file.sessionId ? `打开文件 ${file.name}` : undefined}>
          <FileText size={17} aria-hidden /><span><strong>{file.name}</strong>{file.path !== file.name ? <span className="paw-jev-files__path">{file.path}</span> : null}<small>{file.ownerName ? `${file.ownerName} · ` : ''}{graph ? TASK_STATES[jevTaskStage(file.task, graph)] : '状态待同步'}</small></span><ChevronRight size={13} aria-hidden />
        </button>
        {onOpenFile && file.sessionId ? <button className="paw-jev-files__evidence" type="button" aria-label={`${file.name} 的任务与证据`} title="查看任务与原始证据" onClick={() => setInspector({ kind: 'task', task: file.task })}><ListChecks size={16} aria-hidden /></button> : null}
      </li>)}</ul>{!expanded && files.length > 4 ? <button className="paw-jev-records__more" type="button" onClick={() => setInspector({ kind: 'panel', panel: 'records' })}>查看全部 {files.length} 项文件证据<ChevronRight size={13} aria-hidden /></button> : null}</> : null}
      {history.length ? <><h3>已结束的任务</h3><ul>{(expanded ? history : history.slice(0, 3)).map(task => {
        const owner = room?.participants.find(item => item.id === task.ownerId);
        const activity = !active ? 'static' : task.state === 'failed' ? 'error' : task.state === 'cancelled' ? 'stopped' : recentCompletions.has(task.id) ? 'done' : 'static';
        return <li key={task.id}><button className="paw-jev-record-row" onClick={() => setInspector({ kind: 'task', task })} type="button" data-state={task.state}>
          {owner ? <RoomPlanetAvatar ordinal={owner.ordinal} size={36} activity={activity} decorative /> : <FolderOpen size={22} aria-hidden />}
          <span><strong>{owner ? roomPlanetName(owner.ordinal) : '任务记录'}</strong><span>{graph ? taskTitle(task, graph) : excerpt(task.objective, 36)}</span>
            <small>{task.state === 'done' ? <Check size={12} aria-hidden /> : <CircleAlert size={12} aria-hidden />}{TASK_STATES[task.state]}{task.artifacts.length ? ` · ${task.artifacts.length} 项产物` : ''}</small></span><ChevronRight size={15} aria-hidden />
        </button></li>;
      })}</ul>{!expanded && history.length > 3 ? <button className="paw-jev-records__more" type="button" onClick={() => setInspector({ kind: 'panel', panel: 'records' })}>查看全部 {history.length} 项任务<ChevronRight size={13} aria-hidden /></button> : null}</> : <p className="paw-jev-team__empty">尚无已结束任务。交付与验收回执会保留在这里。</p>}
      {pending.length ? <><h3>接下来与待处理</h3><ul>{pending.map(task => <li key={task.id}><button className="paw-jev-record-row" onClick={() => setInspector({ kind: 'task', task })} type="button">
        <Workflow size={18} aria-hidden /><span><strong>{graph ? taskTitle(task, graph) : excerpt(task.objective, 36)}</strong><small>{graph ? TASK_STATES[jevTaskStage(task, graph)] : '状态待同步'}</small></span><ChevronRight size={15} aria-hidden />
      </button></li>)}</ul></> : null}
      {graph?.historicalTasks?.length ? <details><summary>旧版本任务 · {graph.historicalTasks.length}</summary>
        <ul>{graph.historicalTasks.map(task => <li key={task.id}><button className="paw-jev-record-row" type="button" onClick={() => setInspector({ kind: 'task', task })}>
          <FileText size={17} aria-hidden /><span><strong>{taskTitle(task, graph)}</strong><small>已由新版本接手 · 保留原始记录</small></span><ChevronRight size={13} aria-hidden />
        </button></li>)}</ul></details> : null}
    </section>;
  };
  const inspectedTask = inspector?.kind === 'task' ? graph?.tasks.find(task => task.id === inspector.task.id) ?? graph?.historicalTasks?.find(task => task.id === inspector.task.id) ?? inspector.task : undefined;
  const inspectedFiles = inspectedTask ? fileEvidence([inspectedTask], graph, room) : [];
  const currentVersion = inspectedTask && graph ? jevCurrentTaskVersion(graph, inspectedTask.id) : undefined;
  const inspectedPlan = inspector?.kind === 'plan' ? assignments.find(task => task.key === inspector.task.key) ?? inspector.task : undefined;
  const owner = room?.participants.find(item => item.id === (inspectedTask?.ownerId ?? inspectedPlan?.ownerParticipantId));
  const title = inspector?.kind === 'panel' ? PANEL_TITLES[inspector.panel] : `${owner ? roomPlanetName(owner.ordinal) + ' · ' : ''}${inspectedPlan ? '拟执行任务' : '任务与交付'}`;
  return <>
    {panels.map((kind, index) => <aside className={`paw-jev-team paw-jev-team--${index ? 'right' : 'left'}`} aria-label={index ? '右侧伙伴与交付' : '左侧伙伴与分工'} data-motion={active ? 'active' : 'paused'} data-summary={kind === 'current' && !partners.length && Boolean(graph?.final || graph?.stopped) || undefined} key={index}>
      <div className="paw-jev-team__heading-bar"><button className="paw-jev-team__heading" type="button" aria-label={`展开${PANEL_TITLES[kind]}`} onClick={() => setInspector({ kind: 'panel', panel: kind })}>
        {kind === 'current' || kind === 'plan' ? <UsersRound size={17} aria-hidden /> : kind === 'criteria' ? <ListChecks size={17} aria-hidden /> : <FolderOpen size={17} aria-hidden />}
        <strong>{PANEL_TITLES[kind]}</strong><span>{count(kind)}</span><ChevronRight size={15} aria-hidden />
      </button>{kind === 'current' && partners.length ? toggleCards(`${panelId}-current`) : null}</div>
      <div className="paw-jev-team__progress" aria-label={index === 0 ? '任务完成进度' : '附件与交付文件数量'}>
        {!graph ? <span>{index === 0 ? active ? '正在同步任务进度' : '任务进度待同步' : '文件记录待同步'}</span>
          : index !== 0 ? <span><FileText size={12} aria-hidden />{graph.rootAttachments?.length ? `原始附件 ${graph.rootAttachments.length} 项 · ` : ''}交付文件 {files.length} 项</span>
            : taskCount ? <>
              <strong>已验收 {acceptedCount}/{taskCount}</strong>{!graph.final || executingCount > 0 ? <span>{active ? '执行中' : '上次执行中'} {executingCount}</span> : null}{!graph.final || reviewCount > 0 ? <span>待复核 {reviewCount}</span> : null}
              <progress aria-label="任务验收数" aria-valuetext={`${acceptedCount} 项已验收，共 ${taskCount} 项任务；不是预计耗时进度`} value={acceptedCount} max={taskCount} />
            </> : <span>{planningActive ? <LoaderCircle className="paw-jev-partner__spinner" size={12} aria-hidden /> : null}{planningLabel}</span>}
      </div>
      <div className="paw-jev-team__content" id={kind === 'current' ? `${panelId}-current` : undefined}>{!active && partners.length ? <p className="paw-jev-team__sync">显示上次同步，动态已暂停</p> : null}{renderPanel(kind)}</div>
    </aside>)}
    <Dialog open={Boolean(inspector)} onOpenChange={open => { if (!open) setInspector(null); }}><DialogContent ref={inspectorContent} tabIndex={-1} className="paw-jev-inspector" data-motion={active ? 'active' : 'paused'} onOpenAutoFocus={event => {
        event.preventDefault();
        inspectorContent.current?.focus({ preventScroll: true });
      }}>
      <DialogHeader><DialogTitle>{title}</DialogTitle><DialogDescription>{inspector?.kind === 'panel' ? '来自当前方案、任务状态与执行回执。' : '完整责任、验收与交付依据；执行历史可在伙伴 Session 中查看。'}</DialogDescription></DialogHeader>
      {inspector?.kind === 'panel' ? <>{inspector.panel === 'current' ? <div className="paw-jev-inspector__display">{toggleCards(`${panelId}-dialog-current`)}</div> : null}<div className="paw-jev-inspector__list" id={`${panelId}-dialog-current`}>{renderPanel(inspector.panel, true)}</div></> : <div className="paw-jev-inspector__details">
        <section><h3>任务目标</h3><p>{inspectedTask?.objective ?? inspectedPlan?.objective}</p></section>
        {inspectedTask && !graph ? <p className="paw-jev-inspector__status">状态待同步</p> : null}
        {inspectedTask && graph ? <JevTaskProgress task={inspectedTask} graph={graph} active={active} /> : null}
        {inspectedTask && graph && taskControls && (!graph.activeTaskIds || graph.activeTaskIds.includes(inspectedTask.id)) ? <JevTaskAssignment key={`${graph.graphId}:${inspectedTask.id}`} graph={graph} task={inspectedTask} room={room} controls={taskControls} active={active} /> : null}
        {inspectedTask && graph && taskControls?.revision && (taskControls.revision.pending(graph.graphId, inspectedTask.id)
          || graph.activeTaskIds?.includes(inspectedTask.id) && !graph.final && !graph.stopped)
          ? <JevTaskRevision key={`revision:${graph.graphId}:${inspectedTask.id}`} graph={graph} task={inspectedTask} controls={taskControls.revision} active={active} /> : null}
        {currentVersion && currentVersion.id !== inspectedTask?.id ? <Button variant="quiet" onClick={() => setInspector({ kind: 'task', task: currentVersion })}>查看当前版本任务<ChevronRight size={14} aria-hidden /></Button> : null}
        {(inspectedTask?.expectedOutput || inspectedPlan?.expectedOutput) ? <section><h3>预期产出</h3><p>{inspectedTask?.expectedOutput || inspectedPlan?.expectedOutput}</p></section> : null}
        {(inspectedTask?.acceptance.length || inspectedPlan?.acceptanceCriteria.length) ? <section><h3>验收要求</h3><ul>{(inspectedTask?.acceptance ?? inspectedPlan?.acceptanceCriteria ?? []).map((item, index) => <li key={index}>{item}</li>)}</ul></section> : null}
        {inspectedPlan?.writeTargets.length ? <section><h3>写入范围</h3><ul>{inspectedPlan.writeTargets.map(item => <li key={item}>{item}</li>)}</ul></section> : null}
        {inspectedTask && graph?.edges.some(edge => edge.dependent === inspectedTask.id) ? <section><h3>任务交接来源</h3><ul>{graph.edges.filter(edge => edge.dependent === inspectedTask.id).map(edge => {
          const source = graph.tasks.find(task => task.id === edge.prerequisite);
          return <li key={edge.prerequisite}>{source ? <button className="paw-jev-inspector__dependency" type="button" onClick={() => setInspector({ kind: 'task', task: source })}><JevActivityIcon state={jevTaskStage(source, graph)} active={active} size={15} /><span>{edge.kind === 'context' ? '参考' : '依赖'}：{taskTitle(source, graph)}<small>{TASK_STATES[jevTaskStage(source, graph)]}</small></span><ChevronRight size={14} aria-hidden /></button> : `${edge.prerequisite} · 待同步`}</li>;
        })}</ul></section> : null}
        {inspectedPlan?.dependsOn.length ? <section><h3>前置任务</h3><ul>{inspectedPlan.dependsOn.map(key => { const source = assignments.find(task => task.key === key); return <li key={key}>{source ? <button className="paw-jev-inspector__dependency" type="button" onClick={() => setInspector({ kind: 'plan', task: source })}><span>{excerpt(source.objective, 60)}</span><ChevronRight size={14} aria-hidden /></button> : key}</li>; })}</ul></section> : null}
        {inspectedTask?.result ? <section><h3>结果记录</h3><p>{inspectedTask.result}</p></section> : null}
        {inspectedTask && inspectedTask.artifacts.concat(inspectedTask.evidence).length ? <section><h3>产物与证据</h3>
          {onOpenFile && inspectedFiles.some(file => file.sessionId) ? <p className="paw-jev-inspector__file-note">打开伙伴工作区中的当前文件，交付时的原始证据保留在下方。</p> : null}
          <ul>{[...new Set(inspectedTask.artifacts.concat(inspectedTask.evidence))].map(ref => {
            const file = inspectedFiles.find(item => item.ref === ref);
            return <li key={ref}>{ref}{file?.sessionId && onOpenFile ? <button className="paw-jev-inspector__open-file" type="button" onClick={() => openFile(file)} aria-label={`打开当前文件 ${file.name}`}><FileText size={13} aria-hidden />打开文件</button> : null}</li>;
          })}</ul></section> : null}
        <footer><Button variant="quiet" onClick={() => setInspector({ kind: 'panel', panel: inspectedPlan ? 'plan' : partners.some(work => work.effects.some(effect => effect.request.taskId === inspectedTask?.id)) ? 'current' : 'records' })} leadingIcon={<ArrowLeft size={14} />}>返回列表</Button>{owner ? <Button onClick={() => openParticipant(owner.id)} leadingIcon={<ExternalLink size={14} />}>打开 {roomPlanetName(owner.ordinal)} Session</Button> : null}</footer>
      </div>}
    </DialogContent></Dialog>
  </>;
}

const NO_RECENT_COMPLETIONS: ReadonlySet<string> = new Set();

/** A brief expression for a new receipt, never for an initial/history snapshot. */
function useRecentTaskCompletions(graph: JevSnapshot | null, observing: boolean): ReadonlySet<string> {
  const previous = useRef<{ graphId: string; tasks: Map<string, string> } | null>(null);
  const timers = useRef(new Map<string, number>());
  const [recent, setRecent] = useState<{ graphId: string; tasks: Set<string> }>({ graphId: '', tasks: new Set() });
  useEffect(() => {
    const before = previous.current;
    previous.current = graph && observing ? { graphId: graph.graphId, tasks: new Map(graph.tasks.map(task => [task.id, task.state])) } : null;
    if (!graph || !observing || before?.graphId !== graph.graphId) {
      for (const timer of timers.current.values()) window.clearTimeout(timer);
      timers.current.clear();
      setRecent(current => current.tasks.size ? { graphId: '', tasks: new Set() } : current);
      return;
    }
    for (const task of graph.tasks) {
      if (task.state !== 'done' || !before.tasks.has(task.id) || before.tasks.get(task.id) === 'done') continue;
      window.clearTimeout(timers.current.get(task.id));
      setRecent(current => ({ graphId: graph.graphId, tasks: new Set(current.graphId === graph.graphId ? current.tasks : []).add(task.id) }));
      timers.current.set(task.id, window.setTimeout(() => {
        timers.current.delete(task.id);
        setRecent(current => {
          if (current.graphId !== graph.graphId || !current.tasks.has(task.id)) return current;
          const tasks = new Set(current.tasks); tasks.delete(task.id);
          return { graphId: current.graphId, tasks };
        });
      }, 1000));
    }
  }, [graph, observing]);
  useEffect(() => () => { for (const timer of timers.current.values()) window.clearTimeout(timer); }, []);
  return observing && recent.graphId === graph?.graphId ? recent.tasks : NO_RECENT_COMPLETIONS;
}

function PartnerWindow({ work, graph, projection, active, compact, onOpenParticipant, onInspect }: {
  work: JevPartnerWork; graph: JevSnapshot; projection?: RoomProjectionState; active: boolean; compact: boolean;
  onOpenParticipant: (id: string) => void; onInspect: (task: JevTask) => void;
}) {
  const current = work.effects[0];
  const reduced = useReducedMotion();
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
  return <motion.section className="paw-jev-partner" aria-label={`${name} 当前工作`} data-state={current.executionStatus} data-purpose={purpose} data-compact={compact || undefined} data-exiting={!present || undefined}
    initial={{ opacity: 0, y: reduced || !active ? 0 : 5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: reduced || !active ? 0 : -3 }} transition={{ duration: reduced ? .12 : .18, ease: [.23, 1, .32, 1] }}>
    <header><RoomPlanetAvatar ordinal={work.participant.ordinal} size={32} activity={activity} decorative /><div><strong>{name}</strong>{model ? <span title={model}>{model}</span> : null}</div><button type="button" aria-label={`打开 ${name} Session`} onClick={() => onOpenParticipant(work.participant.id)}><ExternalLink size={14} aria-hidden /></button></header>
    {firstTask ? <button className="paw-jev-partner__task-title" type="button" onClick={() => onInspect(firstTask)}><span>{taskTitle(firstTask, graph)}</span>{tasks.length > 1 ? <small> +{tasks.length - 1}</small> : null}<ChevronRight size={13} aria-hidden /></button> : null}
    <p className="paw-jev-partner__state"><JevActivityIcon size={15} state={graph.stopped ? 'reclaiming' : current.executionStatus === 'unknown' ? 'unknown' : running ? purpose === 'plan' ? 'planning' : purpose === 'verify' ? 'verifying' : purpose === 'synthesize' ? 'synthesizing' : 'running' : current.state === 'sending' ? 'dispatching' : 'queued'} active={spinning || active && present && current.state === 'sending' && !graph.stopped} />
      <span>{compact ? `${name} · ` : ''}{PURPOSES[purpose] || '当前任务'} · {!active ? '上次同步 · ' : ''}{graph.stopped ? '正在停止，等待回执' : STATUS[current.executionStatus] || '状态待核实'}</span>
    </p>
    {firstTask ? <JevTaskProgress task={firstTask} graph={graph} active={active && present} compact /> : null}
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
