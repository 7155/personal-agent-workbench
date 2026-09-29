import { ChevronDown, CircleAlert, GitBranch, Hand, Layers2, RefreshCw, Square, Workflow } from 'lucide-react';
import type { ReactNode } from 'react';
import { useReceiptHighlight } from './use-receipt-highlight';
import { usePresentationMotion } from '@/features/conversation-ui/reading/reading-preferences';
import { MarkdownBody } from '@/features/agent/timeline/MarkdownRenderer';
import type { RoomSummary } from '@/features/rooms/room-types';
import { roomPlanetName } from '@/features/rooms/room-copy';
import { JEV_TASK_STAGE_LABELS, jevAbstention, jevAwaitingPlan, jevLeafTasks, jevPhaseStep, jevRecord, jevStatusLabel, jevTaskCountLabel, jevTaskEffect, jevTaskStage, type JevSnapshot, type JevTaskStage } from './jev-execution';
import { jevAttention, jevMission } from './jev-mission';
import type { JevExecution } from './use-jev-execution';
import { JevPolicyControls } from './JevPolicyControls';
import './jev-execution.css';
import { JevActivityIcon } from './JevActivityIcon';

const STAGES = [ ['route', '判断'], ['plan', '计划'], ['execute', '执行与复核'], ['synthesize', '汇总'], ['final', '答复'] ] as const;
const LABELS: Record<JevTaskStage, string> = { revising: '修改中 · 等待旧执行停止', superseded: '已由新版本接手', queued: '等待派发', dispatching: '正在派发', dispatched: '已派发', running: '正在执行', planning: '正在规划', verifying: '正在复核', synthesizing: '正在汇总', submitted: '执行已返回', review: '等待复核', returned: '退回修改', blocked: '等待条件', unknown: '状态待核实', reclaiming: '回收中 · 等待停止', reassigning: '已停止 · 等待改派', done: '已验收', failed: '未完成', cancelled: '已停止' };


/** A companion to the conversation: backend snapshots own every transition. */
export function JevCompanion({ execution, room, connected = true, active = true, onStop, presentation = 'sidebar', lead, trailing, onOpenTasks, onOpenPlan }: {
  execution: JevExecution; room?: RoomSummary; connected?: boolean; active?: boolean; onStop?: () => void; presentation?: 'sidebar' | 'stage';
  /** Stage only: the goal line owned by the workspace. */
  lead?: ReactNode;
  /** Stage only: workspace controls such as the task-rail toggle. */
  trailing?: ReactNode;
  onOpenTasks?: () => void;
  onOpenPlan?: () => void;
}) {
  if (presentation === 'stage') return <JevMissionHeader execution={execution} room={room} connected={connected} active={active} onStop={onStop} lead={lead} trailing={trailing} onOpenTasks={onOpenTasks} onOpenPlan={onOpenPlan} />;
  const { snapshot: graph } = execution;
  const actorName = (id: string) => { const actor = room?.participants.find(item => item.id === id); if (!actor) return id || '待分配'; const planet = roomPlanetName(actor.ordinal); return planet === actor.displayName ? planet : `${planet} · ${actor.displayName}`; };
  const stale = !connected || Boolean(execution.error);
  const abstention = jevAbstention(graph);
  const abstentionChoice = jevRecord(jevRecord(jevRecord(abstention?.result.receipt).decision).answer).choice;
  const headline = jevStatusLabel(graph, execution.loading);
  const canRetryRoute = Boolean(abstention && graph?.phase === 'route' && graph.graphId === execution.liveSnapshot?.graphId);
  const routeRecovery = canRetryRoute ? <section className="jev-route-recovery" aria-label="继续当前任务">
    <div><strong>路径判断已暂停</strong><p>原任务{graph?.rootAttachments?.length ? '和附件' : ''}已保留。重新判断后会在这条任务中继续。</p></div>
    <button type="button" disabled={execution.routeRetrying || stale} onClick={() => void execution.retryRoute()}>{execution.routeRetrying ? '正在重新判断…' : '重新判断并继续'}</button>
  </section> : null;
  return <aside aria-label="Jev 任务进展" className={`jev-companion jev-companion--${presentation}`} data-motion={active && !stale && !abstention ? 'active' : 'paused'}>
    <details className="jev-companion__disclosure" open>
      <summary><Workflow size={16} aria-hidden /><strong>Jev 任务进展</strong>{stale ? <span className="jev-companion__stale-label" role="status">状态待更新</span> : null}<ChevronDown size={14} aria-hidden /></summary>
      <div className="jev-companion__content">
        <div className="jev-companion__heading"><strong aria-live="polite">{headline}</strong>
          <button aria-label="同步 Jev 任务" disabled={execution.loading} onClick={execution.refresh} type="button"><RefreshCw size={14} aria-hidden /></button>
        </div>
        {execution.items.length > 1 ? <label className="jev-companion__select"><span>当前查看</span><select aria-label="选择 Jev 工作" value={execution.selectedId} onChange={event => execution.selectGraph(event.target.value)}>
          {execution.items.map((item, index) => <option key={item.id} value={item.id}>{item.title || `任务 ${execution.items.length - index}`}{item.stopped ? ' · 已停止' : item.phase === 'final' ? ' · 已结束' : ''}</option>)}
        </select></label> : null}
        {stale ? <p className="jev-companion__warning" role="status"><CircleAlert size={14} aria-hidden />{execution.error || '连接中断。保留上次状态，恢复连接后重新核实。'}</p> : null}
        {routeRecovery}
        {abstention ? <details className="jev-dispatches"><summary>查看调度回执</summary><p>{graph?.phase === 'route' ? '路径判断暂未选出直接执行或先规划，当前没有运行中的执行。' : '最近的调度回执未选出下一步，当前没有运行或待派发的执行。任务仍保留，你可以停止本次任务。'}</p><p>{abstentionChoice === 'insufficient_evidence' ? '调度器选择了“现有证据不足”。' : '回执未提供进一步说明。'}</p></details> : null}
        {graph ? <>
          <JevPhaseRail graph={graph} active={active && !stale && graph.graphId === execution.liveSnapshot?.graphId} />
          <div className="jev-companion__scope"><span>{jevTaskCountLabel(graph)}</span><span>需求版本 {graph.requirementsRevision}</span></div>
          <JevTaskList graph={graph} actorName={actorName} active={active && !stale && !graph.stopped} />
          <JevDispatches graph={graph} actorName={actorName} />
          {graph.modelCards.length ? <JevModelCards cards={graph.modelCards} /> : null}
          {graph.final ? <details className="jev-final" open data-status={graph.final.status}><summary><Layers2 size={15} aria-hidden />{graph.final.status === 'completed' ? '最终答复' : '未完成说明'}</summary><MarkdownBody documentKey={`jev-final:${graph.graphId}`} text={graph.final.content} /></details> : null}
          {execution.busy ? <button className="jev-companion__stop" type="button" disabled={execution.stopping} onClick={onStop ?? (() => void execution.stop())}><Square size={12} fill="currentColor" aria-hidden />{execution.stopping ? '正在请求停止' : '停止当前任务'}</button> : null}
        </> : <p className="jev-companion__empty">在下方描述目标。Jev 会按任务需要判断路径、安排执行、复核结果，再给出答复。</p>}
        <details className="jev-companion__settings"><summary>下一次任务设置</summary><label>推进方式<select aria-label="Jev 推进方式" value={execution.strategy} onChange={event => execution.setStrategy(event.target.value as 'auto' | 'direct' | 'plan')}><option value="auto">自动判断</option><option value="direct">直接执行</option><option value="plan">先规划再执行</option></select></label><JevPolicyControls modelRouting={execution.modelRouting} toolApprovalMode={execution.toolApprovalMode} verificationMode={execution.verificationMode} onModelRouting={execution.setModelRouting} onToolApprovalMode={execution.setToolApprovalMode} onVerificationMode={execution.setVerificationMode} /></details>
      </div>
    </details>
  </aside>;
}

/**
 * The mission header: goal, lifecycle step, one segment per real task and
 * the single thing that needs the user now. Everything is read from the
 * owner snapshot; unknown progress is shown as a stage, never as a percentage.
 */
function JevMissionHeader({ execution, room, connected, active, onStop, lead, trailing, onOpenTasks, onOpenPlan }: {
  execution: JevExecution; room?: RoomSummary; connected: boolean; active: boolean; onStop?: () => void;
  lead?: ReactNode; trailing?: ReactNode; onOpenTasks?: () => void; onOpenPlan?: () => void;
}) {
  const graph = execution.snapshot;
  const stale = !connected || Boolean(execution.error);
  const abstention = jevAbstention(graph);
  const headline = jevStatusLabel(graph, execution.loading);
  const { tasks, counts } = jevMission(graph);
  const historical = Boolean(graph && execution.liveSnapshot && graph.graphId !== execution.liveSnapshot.graphId);
  const fresh = useReceiptHighlight(graph?.graphId ?? '', graph?.tasks.filter(task => task.state === 'done').map(task => `${task.id}:${task.revision}`) ?? [], active && !stale && !historical && !graph?.stopped, 900);
  const attention = historical ? null : jevAttention(graph);
  const canRetryRoute = Boolean(abstention && graph?.phase === 'route' && graph.graphId === execution.liveSnapshot?.graphId);
  const running = Boolean(graph && !graph.stopped && !graph.final && graph.effects.some(effect => effect.operation === 'dispatch' && effect.executionStatus === 'running' && graph.tasks.some(task => task.id === effect.request.taskId && task.revision === effect.request.taskRevision)));
  const motion = usePresentationMotion(active && !stale && !historical && !abstention && running);
  const planTasks = graph?.planApproval && jevAwaitingPlan(graph) ? graph.planApproval.tasks.length : 0;
  const tone = stale ? 'stale' : !graph ? 'idle' : graph.stopped ? 'stopped' : graph.final ? graph.final.status === 'completed' ? 'done' : 'failed'
    : jevAwaitingPlan(graph) ? 'waiting' : abstention ? 'waiting' : running ? 'running' : 'active';
  const summary = [
    counts.running ? `执行中 ${counts.running}` : '',
    counts.reviewing ? `复核 ${counts.reviewing}` : '',
    counts.waiting ? `等待 ${counts.waiting}` : '',
    counts.attention ? `需处理 ${counts.attention}` : '',
    counts.failed ? `未完成 ${counts.failed}` : '',
    counts.stopped ? `已停止 ${counts.stopped}` : '',
  ].filter(Boolean);
  const action = attention && (attention.kind === 'approve' || attention.kind === 'clarify' || attention.kind === 'deferred') && onOpenPlan
    ? { label: attention.kind === 'clarify' ? '去回答' : '查看方案', run: onOpenPlan }
    : attention && ['returned', 'unknown', 'failed', 'stopped'].includes(attention.kind) && onOpenTasks && counts.total
      ? { label: '查看任务', run: onOpenTasks } : null;
  return <aside aria-label="Jev 任务进展" className="jev-companion jev-companion--stage jev-mission" data-motion={motion ? 'active' : 'paused'} data-tone={tone}>
    <div className="jev-mission__top">
      <div className="jev-mission__lead">{lead}</div>
      <div className="jev-mission__tools">
        <button className="jev-companion__sync" aria-label="同步 Jev 任务" title="重新读取任务状态" disabled={execution.loading} onClick={execution.refresh} type="button"><RefreshCw size={15} aria-hidden className={execution.loading ? 'jev-mission__syncing' : undefined} /></button>
        {trailing}
      </div>
    </div>
    <div className="jev-mission__status">
      <details className="jev-companion__disclosure">
        <summary title="查看状态详情、调度回执与下一次任务设置">
          <span className="jev-mission__state-dot" aria-hidden />
          <strong>{headline}</strong>
          {stale ? <span className="jev-companion__stale-label" role="status">状态待更新</span> : null}
          {historical ? <span className="jev-mission__history-label">历史记录</span> : null}
          <ChevronDown size={14} aria-hidden />
        </summary>
        <div className="jev-companion__content"><CompanionDetails execution={execution} room={room} active={active && !historical} stale={stale} onStop={onStop} /></div>
      </details>
      {graph ? <JevPhaseRail graph={graph} active={active && !stale && graph.graphId === execution.liveSnapshot?.graphId} /> : null}
      {counts.total ? <button className="jev-mission__meter" type="button" onClick={onOpenTasks} disabled={!onOpenTasks}
        aria-label={`已验收 ${counts.accepted}/${counts.total}${summary.length ? `，${summary.join('，')}` : ''}。打开任务栏`}>
        <ol aria-hidden>{tasks.map(item => <li key={item.task.id} data-tone={item.tone} data-fresh={fresh.has(`${item.task.id}:${item.task.revision}`) || undefined} title={`${item.task.objective.split(/[。\n]/u)[0].slice(0, 60)} · ${JEV_TASK_STAGE_LABELS[item.stage]}`} />)}</ol>
        <span><strong>已验收 {counts.accepted}/{counts.total}</strong>{summary.map(item => <small key={item}>{item}</small>)}</span>
      </button> : planTasks ? <span className="jev-mission__meter-note">{planTasks} 项拟分工 · 确认后开始</span>
        : graph && jevPhaseStep(graph.phase) === 'plan' && running ? <span className="jev-mission__meter-note">正在拆分任务与依赖</span> : null}
    </div>
    {canRetryRoute ? <section className="jev-route-recovery" aria-label="继续当前任务">
      <CircleAlert size={16} aria-hidden />
      <div><strong>路径判断已暂停</strong><p>原任务{graph?.rootAttachments?.length ? '和附件' : ''}已保留。重新判断后会在这条任务中继续。</p></div>
      <button type="button" disabled={execution.routeRetrying || stale} onClick={() => void execution.retryRoute()}>{execution.routeRetrying ? '正在重新判断…' : '重新判断并继续'}</button>
    </section> : attention ? <section className="jev-mission__attention" data-tone={attention.tone} role={attention.tone === 'action' ? 'status' : undefined} aria-label={attention.title}>
      {attention.tone === 'action' ? <Hand size={16} aria-hidden /> : attention.kind === 'stopped' ? <Square size={14} aria-hidden /> : <CircleAlert size={16} aria-hidden />}
      <div><strong>{attention.title}</strong><p>{attention.detail}</p></div>
      {action ? <button type="button" onClick={action.run}>{action.label}</button> : null}
    </section> : null}
  </aside>;
}

function CompanionDetails({ execution, room, active, stale, onStop }: { execution: JevExecution; room?: RoomSummary; active: boolean; stale: boolean; onStop?: () => void }) {
  const graph = execution.snapshot;
  const actorName = (id: string) => { const actor = room?.participants.find(item => item.id === id); if (!actor) return id || '待分配'; const planet = roomPlanetName(actor.ordinal); return planet === actor.displayName ? planet : `${planet} · ${actor.displayName}`; };
  const abstention = jevAbstention(graph);
  const abstentionChoice = jevRecord(jevRecord(jevRecord(abstention?.result.receipt).decision).answer).choice;
  return <>
    {execution.items.length > 1 ? <label className="jev-companion__select"><span>当前查看</span><select aria-label="选择 Jev 工作" value={execution.selectedId} onChange={event => execution.selectGraph(event.target.value)}>
      {execution.items.map((item, index) => <option key={item.id} value={item.id}>{item.title || `任务 ${execution.items.length - index}`}{item.stopped ? ' · 已停止' : item.phase === 'final' ? ' · 已结束' : ''}</option>)}
    </select></label> : null}
    {stale ? <p className="jev-companion__warning" role="status"><CircleAlert size={14} aria-hidden />{execution.error || '连接中断。保留上次状态，恢复连接后重新核实。'}</p> : null}
    {abstention ? <details className="jev-dispatches"><summary>查看调度回执</summary><p>{graph?.phase === 'route' ? '路径判断暂未选出直接执行或先规划，当前没有运行中的执行。' : '最近的调度回执未选出下一步，当前没有运行或待派发的执行。任务仍保留，你可以停止本次任务。'}</p><p>{abstentionChoice === 'insufficient_evidence' ? '调度器选择了“现有证据不足”。' : '回执未提供进一步说明。'}</p></details> : null}
    {graph ? <>
      <div className="jev-companion__scope"><span>{jevTaskCountLabel(graph)}</span><span>需求版本 {graph.requirementsRevision}</span></div>
      <JevTaskList graph={graph} actorName={actorName} active={active && !stale && !graph.stopped} />
      <JevDispatches graph={graph} actorName={actorName} />
      {graph.modelCards.length ? <JevModelCards cards={graph.modelCards} /> : null}
      {execution.busy ? <button className="jev-companion__stop" type="button" disabled={execution.stopping} onClick={onStop ?? (() => void execution.stop())}><Square size={12} fill="currentColor" aria-hidden />{execution.stopping ? '正在请求停止' : '停止当前任务'}</button> : null}
    </> : <p className="jev-companion__empty">在下方描述目标。Jev 会按任务需要判断路径、安排执行、复核结果，再给出答复。</p>}
    <details className="jev-companion__settings"><summary>下一次任务设置</summary><label>推进方式<select aria-label="Jev 推进方式" value={execution.strategy} onChange={event => execution.setStrategy(event.target.value as 'auto' | 'direct' | 'plan')}><option value="auto">自动判断</option><option value="direct">直接执行</option><option value="plan">先规划再执行</option></select></label><JevPolicyControls modelRouting={execution.modelRouting} toolApprovalMode={execution.toolApprovalMode} verificationMode={execution.verificationMode} onModelRouting={execution.setModelRouting} onToolApprovalMode={execution.setToolApprovalMode} onVerificationMode={execution.setVerificationMode} /></details>
  </>;
}

function JevTaskList({ graph, actorName, active }: { graph: JevSnapshot; actorName: (id: string) => string; active: boolean }) {
  return <ol className="jev-task-list" aria-label="任务依赖与负责人">
    {jevLeafTasks(graph).map(task => {
      const state = jevTaskStage(task, graph);
      const effect = jevTaskEffect(task, graph);
      const currentOwner = effect?.executionStatus === 'running' && typeof effect.request.ownerId === 'string' ? effect.request.ownerId : task.ownerId;
      const prerequisites = graph.edges.filter(edge => edge.dependent === task.id);
      const blocked = graph.blocked.find(item => item.taskId === task.id);
      return <li className="jev-task" data-state={state} key={task.id}>
        <div className="jev-task__line"><span className="jev-task__mark" key={`${task.id}:${state}`} aria-hidden><JevActivityIcon state={state} active={active} size={15} /></span><strong>{task.objective || '未命名任务'}</strong></div>
        <div className="jev-task__status"><span>{LABELS[state]}</span><span>{actorName(currentOwner)}</span>{currentOwner !== task.ownerId ? <span>任务负责人：{actorName(task.ownerId)}</span> : null}</div>
        {prerequisites.length ? <ul className="jev-task__dependencies" aria-label={`${task.objective}的依赖`}>{prerequisites.map(edge => <li key={`${edge.kind}:${edge.prerequisite}`}><GitBranch size={12} aria-hidden />{edge.kind === 'context' ? '参考' : '等待'}：{graph.tasks.find(item => item.id === edge.prerequisite)?.objective || edge.prerequisite}</li>)}</ul> : null}
        {task.result || task.acceptance.length || task.artifacts.length || blocked?.reasons.length ? <details className="jev-task__evidence"><summary>结果与验收依据</summary>
          {task.result ? <p>{task.result}</p> : null}
          {task.expectedOutput ? <p><strong>交付：</strong>{task.expectedOutput}</p> : null}
          {task.acceptance.length ? <ul>{task.acceptance.map(item => <li key={item}>{item}</li>)}</ul> : null}
          {blocked?.reasons.length ? <p>等待条件：{blocked.reasons.map(reason => REASONS[reason] || reason).join('、')}</p> : null}
          {task.artifacts.concat(task.evidence).length ? <ul>{[...new Set([...task.artifacts, ...task.evidence])].map(item => <li key={item}>{item}</li>)}</ul> : null}
        </details> : null}
      </li>;
    })}
  </ol>;
}

function JevPhaseRail({ graph, active = true }: { graph: JevSnapshot; active?: boolean }) {
  const phase = jevPhaseStep(graph.phase);
  const stages = STAGES.filter(([id]) => id === phase || id === 'final'
    || id === 'plan' && (graph.planApproval || graph.effects.some(effect => effect.request.purpose === 'plan'))
    || id === 'execute' && (graph.planApproval?.tasks.length || graph.tasks.some(task => task.parentId) || graph.effects.some(effect => effect.request.purpose === 'execute'))
    || id === 'synthesize' && graph.effects.some(effect => effect.request.purpose === 'synthesize'));
  return <ol aria-label="任务阶段" className="jev-phase-rail" data-stopped={graph.stopped || undefined}>
    {stages.map(([id, label]) => <li aria-current={!graph.stopped && phase === id ? 'step' : undefined} data-current={phase === id || undefined} key={id}>
      <span className="jev-phase-rail__mark" aria-hidden><JevActivityIcon size={13} state={id === 'final' ? graph.final?.status === 'completed' ? 'done' : graph.final ? 'failed' : 'queued' : id === 'route' || id === 'plan' ? 'planning' : id === 'synthesize' ? 'synthesizing' : 'running'} active={active && !graph.stopped && phase === id && graph.effects.some(effect => effect.operation === 'dispatch' && effect.executionStatus === 'running' && graph.tasks.some(task => task.id === effect.request.taskId && task.revision === effect.request.taskRevision))} /></span><span>{label}</span>
    </li>)}
  </ol>;
}

function JevDispatches({ graph, actorName }: { graph: JevSnapshot; actorName: (id: string) => string }) {
  const effects = graph.effects.filter(item => item.operation === 'dispatch');
  if (!effects.length) return null;
  return <details className="jev-dispatches"><summary>执行交接 · {effects.length}</summary><ol>{effects.map(effect => {
    const purpose = String(effect.request.purpose || 'execute');
    return <li key={effect.effectId} data-state={effect.executionStatus || effect.state}><JevActivityIcon size={15} state={effect.executionStatus === 'running' ? purpose === 'plan' ? 'planning' : purpose === 'verify' ? 'verifying' : purpose === 'synthesize' ? 'synthesizing' : 'running' : effect.state === 'sending' ? 'dispatching' : effect.state === 'rejected' ? 'failed' : effect.executionStatus === 'unknown' || effect.state === 'unknown' ? 'unknown' : effect.executionStatus === 'drained' ? 'submitted' : 'queued'} active={!graph.stopped} /><div><strong>{({ plan: '计划', execute: '执行', verify: '复核', synthesize: '汇总' } as Record<string, string>)[purpose] || purpose}</strong><span>{actorName(String(effect.request.ownerId || ''))}</span></div><small>{({ running: '执行中', drained: '已返回', admitted: '已接收', prepared: '待发送', pending: '待发送', sending: '发送中', accepted: '已接收', rejected: '未接收', not_sent: '未发送', unknown: '回执待核实' } as Record<string, string>)[effect.executionStatus || effect.state] || '状态待核实'}</small></li>;
  })}</ol></details>;
}

const REASONS: Record<string, string> = { prerequisite_not_done: '前置任务尚未验收', dependencies: '前置任务尚未完成', execution_unknown: '执行回执待核实', owner_unavailable: '等待负责人可用', parallel_capacity: '等待执行空位', active_execution: '等待执行结束', not_queued: '等待任务状态更新' };

function JevModelCards({ cards }: { cards: Record<string, unknown>[] }) {
  const lines = (value: unknown) => Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
  return <details className="jev-model-cards"><summary>模型分工依据</summary><p>模型定位、社区经验与本次分工策略分别列出；不是本机质量测评。</p>{cards.map(card => <details key={String(card.modelId)}><summary>{String(card.name || card.modelId)} · {String(card.role || '')}</summary><p>{String(card.routingGuidance || '')}</p><ul>{lines(card.strengths).map(value => <li key={value}>{value}</li>)}</ul><p>适用边界</p><ul>{lines(card.limitations).map(value => <li key={value}>{value}</li>)}</ul><ul>{(Array.isArray(card.evidence) ? card.evidence : []).map(jevRecord).map((source, index) => {
    const url = typeof source.url === 'string' && /^https?:\/\//.test(source.url) ? source.url : '';
    const label = source.sourceKind === 'official' ? '官方定位' : source.sourceKind === 'community' ? '社区经验' : '分工策略';
    return <li key={index}><span>{label}：{String(source.summary || '')}</span>{url ? <a href={url} target="_blank" rel="noreferrer">{String(source.title || '查看来源')}</a> : null}</li>;
  })}</ul><small>核对日期 {String(card.checkedAt || '未提供')}</small></details>)}</details>;
}
