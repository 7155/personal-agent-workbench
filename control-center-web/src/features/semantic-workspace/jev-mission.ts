import { jevAbstention, jevAwaitingPlan, jevLeafTasks, jevTaskStage, type JevSnapshot, type JevTask, type JevTaskStage } from './jev-execution';

/**
 * One read-only projection of a Jev graph for the mission header and task
 * rail. It only groups server stages; it never infers progress, time or a
 * percentage from tool activity.
 */
export type JevLane = 'active' | 'attention' | 'waiting' | 'ended';
/** Visual tone of one task segment. Every tone is paired with a text label. */
export type JevTone = 'done' | 'active' | 'review' | 'attention' | 'waiting' | 'failed' | 'stopped';

export interface JevMissionTask {
  task: JevTask;
  stage: JevTaskStage;
  lane: JevLane;
  tone: JevTone;
  /** Prerequisites that are not accepted yet, from committed graph edges. */
  waitingOn: JevTask[];
  /** Server blocked reasons, translated for reading. */
  reasons: string[];
}

export interface JevMissionCounts {
  total: number; accepted: number; running: number; reviewing: number;
  waiting: number; attention: number; failed: number; stopped: number;
}

export const JEV_WAIT_REASONS: Record<string, string> = {
  prerequisite_not_done: '前置任务尚未验收', dependencies: '前置任务尚未完成', execution_unknown: '执行回执待核实',
  owner_unavailable: '等待负责人可用', parallel_capacity: '等待执行空位', active_execution: '等待执行结束', not_queued: '等待任务状态更新',
};

const ACTIVE = new Set<JevTaskStage>(['dispatching', 'dispatched', 'running', 'planning', 'verifying', 'synthesizing', 'submitted', 'revising', 'reclaiming', 'reassigning']);
const ENDED = new Set<JevTaskStage>(['done', 'failed', 'cancelled', 'superseded']);

export function jevLane(stage: JevTaskStage): JevLane {
  if (ENDED.has(stage)) return 'ended';
  if (stage === 'returned' || stage === 'unknown') return 'attention';
  if (ACTIVE.has(stage)) return 'active';
  return 'waiting';
}

export function jevTone(stage: JevTaskStage): JevTone {
  if (stage === 'done') return 'done';
  if (stage === 'failed') return 'failed';
  if (stage === 'cancelled' || stage === 'superseded') return 'stopped';
  if (stage === 'returned' || stage === 'unknown') return 'attention';
  if (stage === 'review' || stage === 'verifying' || stage === 'submitted') return 'review';
  if (stage === 'queued' || stage === 'blocked') return 'waiting';
  return 'active';
}

export function jevMission(graph: JevSnapshot | null): { tasks: JevMissionTask[]; counts: JevMissionCounts } {
  const leaves = jevLeafTasks(graph);
  const tasks = graph ? leaves.map((task): JevMissionTask => {
    const stage = jevTaskStage(task, graph);
    const waitingOn = ENDED.has(stage) ? [] : graph.edges
      .filter(edge => edge.dependent === task.id && edge.kind !== 'context')
      .map(edge => graph.tasks.find(item => item.id === edge.prerequisite))
      .filter((item): item is JevTask => Boolean(item && jevTaskStage(item, graph) !== 'done'));
    const reasons = (graph.blocked.find(item => item.taskId === task.id)?.reasons ?? []).map(reason => JEV_WAIT_REASONS[reason] || reason);
    return { task, stage, lane: jevLane(stage), tone: jevTone(stage), waitingOn, reasons };
  }) : [];
  const count = (test: (item: JevMissionTask) => boolean) => tasks.filter(test).length;
  return { tasks, counts: {
    total: tasks.length,
    accepted: count(item => item.stage === 'done'),
    running: count(item => ['running', 'planning', 'synthesizing'].includes(item.stage)),
    reviewing: count(item => ['review', 'verifying', 'submitted'].includes(item.stage)),
    waiting: count(item => ['queued', 'blocked', 'dispatching', 'dispatched', 'reassigning', 'reclaiming', 'revising'].includes(item.stage)),
    attention: count(item => item.lane === 'attention'),
    failed: count(item => item.stage === 'failed'),
    stopped: count(item => item.stage === 'cancelled'),
  } };
}

export type JevAttention = { kind: 'clarify' | 'approve' | 'deferred' | 'abstained' | 'returned' | 'unknown' | 'failed' | 'stopped' | 'scheduling'; tone: 'action' | 'warning' | 'danger' | 'info'; title: string; detail: string };

/**
 * What the user should look at now, derived from owner state only. Route
 * abstention has its own recovery control and is handled by the caller.
 */
export function jevAttention(graph: JevSnapshot | null): JevAttention | null {
  if (!graph) return null;
  const { counts } = jevMission(graph);
  if (graph.final) {
    if (graph.final.status === 'completed') return null;
    const first = graph.final.content.split('\n').find(line => line.trim())?.trim() ?? '';
    return { kind: 'failed', tone: 'danger', title: '本次未完成', detail: first || '最终答复说明了未完成的原因。' };
  }
  if (graph.stopped) return { kind: 'stopped', tone: 'info', title: '任务已停止', detail: counts.accepted ? `已验收的 ${counts.accepted} 项结果保留在任务栏。` : '已有的执行记录保留，可以重新发送目标继续。' };
  if (jevAwaitingPlan(graph)) {
    const status = graph.planApproval?.status || graph.phase;
    if (status === 'awaiting_input') return { kind: 'clarify', tone: 'action', title: '需要你补充目标与范围', detail: '回答问题后，Jev 会形成完整方案。' };
    if (status === 'deferred') return { kind: 'deferred', tone: 'info', title: '方案已保留，暂未执行', detail: '可以重新查看方案并开始执行。' };
    return { kind: 'approve', tone: 'action', title: '方案等待你确认', detail: `${graph.planApproval?.tasks.length ?? 0} 项任务 · 确认后才开始执行` };
  }
  if (jevAbstention(graph) && graph.phase !== 'route') return { kind: 'abstained', tone: 'warning', title: '调度回执未选出下一步', detail: '当前没有执行中的伙伴。可以补充说明，或停止本次任务。' };
  if (counts.attention) {
    const returned = jevMission(graph).tasks.filter(item => item.stage === 'returned').length;
    return returned
      ? { kind: 'returned', tone: 'warning', title: `${returned} 项任务已退回修改`, detail: '复核发现未满足的验收要求，原负责人正在返修或等待重新派发。' }
      : { kind: 'unknown', tone: 'warning', title: '有执行回执待核实', detail: '同步后会以服务端回执为准，不会重复执行。' };
  }
  return null;
}
