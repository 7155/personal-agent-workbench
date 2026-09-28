import { useEffect, useId, useState } from 'react';
import { Check, ChevronRight, CircleAlert, ClipboardList, MessageCircle, Pencil, Play } from 'lucide-react';
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from '@/components/primitives';
import { RoomPlanetAvatar } from '@/features/rooms/RoomPlanetAvatar';
import { roomPlanetName } from '@/features/rooms/room-copy';
import type { RoomSummary } from '@/features/rooms/room-types';
import { publicAgentErrorText } from '@/features/agent/public-error';
import type { JevExecution } from './use-jev-execution';
import { JEV_TASK_STAGE_LABELS, jevTaskStage, type JevPlanAction, type JevPlanTask, type JevSnapshot } from './jev-execution';
import './jev-plan-review.css';

/** A review of one owner-persisted proposal; buttons never schedule locally. */
export function JevPlanReview({ execution, room, onAdjust }: { execution: JevExecution; room: RoomSummary; onAdjust: () => void }) {
  const graph = execution.liveSnapshot;
  const plan = graph?.planApproval;
  const [error, setError] = useState('');
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState(false);
  const questionId = useId();
  useEffect(() => { setError(''); setAnswers({}); setEditing(false); }, [graph?.graphId, plan?.planHash]);
  if (!graph || !plan || plan.status === 'planning') return null;
  const approved = plan.status === 'approved';
  const questions = plan.clarifications;
  const busy = Boolean(execution.planSending || execution.pendingPlan || execution.creating);
  const act = async (action: JevPlanAction, message?: string) => {
    setError('');
    try { await execution.decidePlan(action, message); }
    catch (reason) { setError(publicAgentErrorText(reason, '方案操作尚未确认，请重新同步后核实。')); execution.refresh(); }
  };
  if (approved) return <Dialog>
    <DialogTrigger asChild><button className="jev-plan-review__approved-trigger" type="button" aria-label={`执行方案已确认 · 版本 ${plan.requirementsRevision}`} title="查看完整方案、分工与验收标准"><Check size={15} aria-hidden /><span>执行方案已确认 · 版本 {plan.requirementsRevision}</span><small>{plan.tasks.length} 项任务{waveCount(plan.tasks) > 1 ? ` · 分 ${waveCount(plan.tasks)} 步` : ''}</small><em>查看完整方案</em><ChevronRight size={14} aria-hidden /></button></DialogTrigger>
    <DialogContent className="jev-plan-review jev-plan-review__dialog">
      <div className="jev-plan-review__dialog-heading">
        <DialogTitle>已确认的执行方案</DialogTitle>
        <DialogDescription>版本 {plan.requirementsRevision} · 查看任务分工、前置依赖和交付标准。</DialogDescription>
      </div>
      <div className="jev-plan-review__dialog-body" role="region" aria-label="方案分工与验收标准" tabIndex={0}>
        <PlanTasks room={room} plan={plan} graph={graph} />
      </div>
    </DialogContent>
  </Dialog>;
  if (graph.stopped || graph.final) return null;
  return <section aria-label={questions.length ? '目标澄清' : '整体执行方案'} className="jev-plan-review" data-status={plan.status}>
    <header>{questions.length ? <MessageCircle size={18} aria-hidden /> : <ClipboardList size={18} aria-hidden />}<div><strong>{questions.length ? '先明确目标与范围' : '整体执行方案'}</strong><span>{plan.status === 'deferred' ? '已保留，暂未执行' : questions.length ? '回答后继续形成方案' : `方案版本 ${plan.requirementsRevision} · 待你确认`}</span></div></header>
    <p className="jev-plan-review__objective">{(graph.tasks.find(task => !task.parentId)?.objective || room.description || room.title).trim().split('\n')[0]}</p>
    {questions.length ? <form onSubmit={event => { event.preventDefault(); const message = questions.map(question => `${question.question}\n${answers[question.id]?.trim() || '暂未补充'}`).join('\n\n'); void act('adjust_plan', message); }}>
      {questions.map((question, index) => <div className="jev-plan-review__question" key={question.id}><label htmlFor={`${questionId}-${index}`}>{question.question}</label>{question.options.length ? <span role="group" aria-label={`${question.question}的选项`}>{question.options.map(option => <button type="button" aria-pressed={answers[question.id] === option} key={option} onClick={() => setAnswers(previous => ({ ...previous, [question.id]: option }))}>{option}</button>)}</span> : null}<textarea id={`${questionId}-${index}`} value={answers[question.id] || ''} onChange={event => setAnswers(previous => ({ ...previous, [question.id]: event.target.value }))} rows={2} /></div>)}
      <div className="jev-plan-review__actions"><span>执行会在方案确认后开始。</span><button className="jev-plan-review__primary" disabled={busy || !Object.values(answers).some(answer => answer.trim())} type="submit">提交补充</button><button type="button" disabled={busy || plan.status === 'deferred'} onClick={() => void act('defer_plan')}>暂不执行</button></div>
    </form> : <>
      <PlanTasks room={room} plan={plan} />
      <div className="jev-plan-review__actions"><span>{editing ? '在下方输入框写下要调整的内容，发送后 Jev 会生成修订方案。' : '确认即授权本方案中的全部任务执行，执行期间不再逐项审批工具。'}</span><button className="jev-plan-review__primary" disabled={busy || !plan.planHash || !plan.tasks.length} type="button" onClick={() => void act('approve_plan')}><Play size={13} aria-hidden />{execution.planSending === 'approve_plan' ? '正在确认' : '开始执行'}</button><button disabled={busy} type="button" onClick={() => { setEditing(true); onAdjust(); }}><Pencil size={13} aria-hidden />调整方案</button><button disabled={busy || plan.status === 'deferred'} type="button" onClick={() => void act('defer_plan')}>暂不执行</button></div>
    </>}
    {error ? <p className="jev-plan-review__error" role="alert"><CircleAlert size={14} aria-hidden />{error}</p> : null}
  </section>;
}

function PlanTasks({ room, plan, graph }: { room: RoomSummary; plan: NonNullable<JevSnapshot['planApproval']>; graph?: JevSnapshot }) {
  const waves = jevPlanWaves(plan.tasks);
  const number = (key: string) => plan.tasks.findIndex(task => task.key === key) + 1;
  return <>
    <section className="jev-plan-review__sequence" aria-label="任务执行顺序">
      <strong>分工与执行顺序</strong>
      <p>{plan.tasks.length} 项任务 · 同一步的任务同时开始；下一步等上一步验收后再开始。</p>
      {waves ? <ol>{waves.map((wave, index) => <li key={wave[0].key} data-parallel={wave.length > 1 || undefined}><span>第 {index + 1} 步{wave.length > 1 ? ` · ${wave.length} 项并行` : ''}{index ? ` · 等第 ${index} 步验收` : ' · 确认后开始'}</span><div>{wave.map(task => { const owner = room.participants.find(item => item.id === task.ownerParticipantId); return <span key={task.key}>{owner ? <RoomPlanetAvatar ordinal={owner.ordinal} size={16} decorative /> : null}任务 {number(task.key)} · {planTaskTitle(task, room)}</span>; })}</div></li>)}</ol> : <p>依赖顺序尚未确认，请调整方案后再执行。</p>}
    </section>
    <ol className="jev-plan-review__tasks">{plan.tasks.map(task => {
    // Older approved plans left assignment to dispatch. Show a uniquely bound
    // current owner separately; do not invent one for duplicate objectives.
    const assigned = graph?.tasks.filter(item => item.parentId && item.objective === task.objective) ?? [];
    const current = assigned.length === 1 ? assigned[0] : undefined;
    const ownerId = task.ownerParticipantId || current?.ownerId;
    const participant = room.participants.find(item => item.id === ownerId);
    const stage = current && graph ? jevTaskStage(current, graph) : undefined;
    const currentOwner = current && room.participants.find(item => item.id === current.ownerId);
    const status = stage ? graph?.stopped && !['done', 'failed', 'cancelled'].includes(stage) ? '已请求停止，回执待核实' : JEV_TASK_STAGE_LABELS[stage] : '任务状态待同步';
    return <li key={task.key}>
      <div className="jev-plan-review__task-title">{participant ? <RoomPlanetAvatar ordinal={participant.ordinal} size={26} decorative /> : <ClipboardList size={19} aria-hidden />}<strong><small>任务 {number(task.key)}</small>{planTaskTitle(task, room)}</strong><span>{participant ? `${task.ownerParticipantId ? '' : '当前 · '}${roomPlanetName(participant.ordinal)}` : '执行时分配'}</span></div>
      {graph ? <p className="jev-plan-review__task-status" data-state={stage} aria-label={`任务 ${number(task.key)} 当前状态`}>{status}{currentOwner && current?.ownerId !== ownerId ? ` · 当前负责人 ${roomPlanetName(currentOwner.ordinal)}` : ''}</p> : null}
      <p>交付：{task.expectedOutput}</p><p className="jev-plan-review__dependency">{task.dependsOn.length ? `${graph ? '前置依赖：' : '等待 '}${task.dependsOn.map(key => `任务 ${number(key) || key}`).join('、')}${graph ? '' : ' 验收后开始'}` : graph ? '无前置依赖' : '方案确认后可开始'}</p>
      <details><summary>完整任务与验收标准 · {task.acceptanceCriteria.length} 项</summary><p>{task.objective}</p><ul aria-label={`${task.objective}的验收标准`}>{task.acceptanceCriteria.map(criterion => <li key={criterion}>{criterion}</li>)}</ul>{task.writeTargets.length ? <p>改动范围：{task.writeTargets.join('、')}</p> : null}{task.contextRefs.map(ref => <p key={ref}>{ref}</p>)}</details>
    </li>;
  })}</ol></>;
}

function planTaskTitle(task: JevPlanTask, room: RoomSummary): string {
  const named = room.participants.reduce((text, participant) => text.replaceAll(participant.id, roomPlanetName(participant.ordinal)), task.objective);
  return named.split(/[。；;\n]/u)[0]
    .split(/[，,](?:simple|routine|complex|critical|由宿主)/iu)[0]
    .replace(/[（(](?:simple|routine|complex|critical)[，,]\s*(?:建议\s*)?(?:宿主按\s*)?(?:Astra|Sol|Luna)(?:\s+(?:low|medium|high|xhigh|max|ultra))?(?:\s*调度)?[）)]/giu, '')
    .replace(/^由.{1,20}?伙伴(?:独立)?/u, '').trim() || task.key;
}

/** A display of approved dependencies, never a second scheduler. */
function waveCount(tasks: JevPlanTask[]) { return jevPlanWaves(tasks)?.length ?? 0; }

export function jevPlanWaves(tasks: JevPlanTask[]): JevPlanTask[][] | null {
  const remaining = new Map(tasks.map(task => [task.key, task]));
  const complete = new Set<string>();
  const waves: JevPlanTask[][] = [];
  while (remaining.size) {
    const ready = [...remaining.values()].filter(task => task.dependsOn.every(key => complete.has(key)));
    if (!ready.length) return null;
    waves.push(ready);
    ready.forEach(task => { remaining.delete(task.key); complete.add(task.key); });
  }
  return waves;
}
