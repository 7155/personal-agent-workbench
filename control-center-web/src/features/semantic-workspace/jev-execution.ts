import type { ControlRequest, ControlTransport } from '@/platform/transport';
import { observeRequest } from './organization-request';

export type JevStrategy = 'auto' | 'direct' | 'plan';
export type JevModelRouting = 'balanced' | 'participant';
export type JevToolApproval = 'dispatch' | 'jev_dangerous';
export type JevVerificationMode = 'auto' | 'independent';
const JEV_PHASES = ['route', 'plan', 'awaiting_input', 'awaiting_approval', 'deferred', 'execute', 'synthesize', 'final'] as const;
export type JevPhase = typeof JEV_PHASES[number];
/** Waiting for the user is still the plan step, while preserving lifecycle truth. */
export const jevPhaseStep = (phase: JevPhase) => phase === 'awaiting_input' || phase === 'awaiting_approval' || phase === 'deferred' ? 'plan' : phase;
export interface JevTask {
  taskHash?: string;
  id: string; state: string; revision: number; ownerId: string; parentId: string;
  objective: string; expectedOutput: string; acceptance: string[]; result: string;
  artifacts: string[]; evidence: string[]; acceptedTurnId: string;
}
export interface JevEffect {
  effectId: string; operation: string; state: string;
  executionStatus: string;
  request: Record<string, unknown>; receipt: Record<string, unknown>;
}
export interface JevGraphItem {
  id: string; roomId: string; rootId: string; title: string; phase: string;
  clientMessageId: string;
  stopped: boolean; createdAtMs: number;
}
export interface JevPlanTask {
  key: string; objective: string; expectedOutput: string; acceptanceCriteria: string[];
  dependsOn: string[]; contextRefs: string[]; writeTargets: string[]; ownerParticipantId: string;
}
export interface JevPlanApproval {
  status: 'planning' | 'awaiting_input' | 'awaiting_approval' | 'deferred' | 'approved';
  planHash: string; requirementsRevision: number; lastActionClientMessageId: string;
  tasks: JevPlanTask[]; clarifications: { id: string; question: string; options: string[] }[];
}
export interface JevSnapshot {
  graphId: string; rootId: string; version: string; phase: JevPhase; stopped: boolean;
  requirementsRevision: number; tasks: JevTask[];
  edges: { prerequisite: string; dependent: string; kind: string }[];
  ready: string[]; running: string[]; review: string[];
  blocked: { taskId: string; reasons: string[] }[]; effects: JevEffect[];
  events: { id: string; kind: string; state: string; result: Record<string, unknown> }[];
  final: { content: string; status: string; evidence: string[] } | null;
  modelCards: Record<string, unknown>[];
  planApproval: JevPlanApproval | null;
  activeTaskIds?: string[];
  historicalTasks?: JevTask[];
  currentRootObjective?: string;
  revisions?: { revisionId: string; status: string; changedTaskId: string; affectedTaskIds: string[]; retainedAcceptedTaskIds: string[]; successorTaskIds: string[]; successors?: Record<string, string> }[];
  reclaims?: { reclaimId: string; taskId: string; taskRevision: number; dispatchId: string; targetParticipantId: string; stage: 'awaiting_stop' | 'awaiting_assignment' }[];
}

export const jevRecord = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
/** Follow only committed server mappings; titles and owners never imply lineage. */
export function jevCurrentTaskVersion(graph: JevSnapshot, taskId: string): JevTask | undefined {
  const successors = new Map<string, string>();
  for (const revision of graph.revisions ?? []) if (revision.status === 'applied') {
    for (const [oldId, newId] of Object.entries(revision.successors ?? {})) successors.set(oldId, newId);
  }
  const seen = new Set<string>();
  let current = taskId;
  while (!seen.has(current)) {
    const task = graph.tasks.find(item => item.id === current);
    if (task) return task;
    seen.add(current);
    const next = successors.get(current); if (!next) return;
    current = next;
  }
}
const text = (value: unknown) => typeof value === 'string' ? value : '';
const list = (value: unknown): unknown[] => Array.isArray(value) ? value : [];
const strings = (value: unknown) => list(value).filter((item): item is string => typeof item === 'string');
function resultObject(value: unknown) {
  if (typeof value !== 'string') return jevRecord(value);
  try { return jevRecord(JSON.parse(value)); } catch { return {}; }
}

export function parseJevList(value: unknown, roomId: string): JevGraphItem[] {
  const data = jevRecord(value);
  if (data.ok !== true || data.mode !== 'jev' || !Array.isArray(data.items)) throw new Error('未收到有效的 Jev 工作记录，请重新同步。');
  return data.items.map(jevRecord).filter(item => text(item.room_id) === roomId && text(item.graph_id)).map(item => ({
    id: text(item.graph_id), roomId, rootId: text(item.root_turn_id), title: text(item.objective ?? item.title),
    clientMessageId: text(item.clientMessageId ?? item.client_message_id),
    phase: text(item.phase), stopped: item.stopped === true || item.stopped === 1,
    createdAtMs: typeof item.created_at_ms === 'number' ? item.created_at_ms : 0,
  }));
}

export function parseJevSnapshot(value: unknown, graphId: string): JevSnapshot {
  const data = jevRecord(value);
  if (data.ok !== true || data.mode !== 'jev' || data.graphId !== graphId || !text(data.snapshotVersion)
    || !JEV_PHASES.includes(data.phase as JevPhase) || !Array.isArray(data.tasks)) {
    throw new Error('任务状态没有与当前工作匹配，请重新同步。');
  }
  const final = jevRecord(data.final);
  const plan = jevRecord(data.planApproval);
  const proposal = jevRecord(plan.proposal);
  const allTasks = data.tasks.map(jevRecord).map(task => ({
    id: text(task.id), state: text(task.state), revision: Number(task.revision) || 0, taskHash: text(task.taskHash),
    ownerId: text(task.owner_id), parentId: text(task.parent_id), objective: text(task.objective),
    expectedOutput: text(task.expected_output), acceptance: strings(task.acceptance),
    result: text(task.result), artifacts: strings(task.artifacts), evidence: strings(task.evidence), acceptedTurnId: text(task.accepted_turn_id),
  }));
  const activeTaskIds = Array.isArray(data.activeTaskIds) ? strings(data.activeTaskIds) : undefined;
  return {
    graphId, rootId: text(data.rootId), version: text(data.snapshotVersion), phase: data.phase as JevPhase,
    stopped: data.stopped === true, requirementsRevision: Number(data.requirementsRevision) || 0,
    ready: strings(data.ready), running: strings(data.running), review: strings(data.review),
    tasks: activeTaskIds ? allTasks.filter(task => activeTaskIds.includes(task.id)) : allTasks,
    activeTaskIds, historicalTasks: activeTaskIds ? allTasks.filter(task => !activeTaskIds.includes(task.id)) : [],
    currentRootObjective: text(data.currentRootObjective),
    revisions: list(data.revisions).map(jevRecord).map(item => ({ revisionId: text(item.revisionId), status: text(item.status),
      changedTaskId: text(item.changedTaskId), affectedTaskIds: strings(item.affectedTaskIds), retainedAcceptedTaskIds: strings(item.retainedAcceptedTaskIds), successorTaskIds: strings(item.successorTaskIds),
      successors: Object.fromEntries(Object.entries(jevRecord(item.successors)).filter((entry): entry is [string, string] => typeof entry[1] === 'string')) })),
    edges: list(data.edges).map(jevRecord).map(edge => ({ prerequisite: text(edge.prerequisite), dependent: text(edge.dependent), kind: text(edge.kind) })),
    blocked: list(data.blocked).map(jevRecord).map(item => ({ taskId: text(item.taskId), reasons: strings(item.reasons) })),
    effects: list(data.effects).map(jevRecord).map(effect => ({ effectId: text(effect.effectId), operation: text(effect.operation), state: text(effect.state), executionStatus: text(effect.executionStatus), request: jevRecord(effect.request), receipt: jevRecord(effect.receipt) })),
    reclaims: list(data.reclaims).map(jevRecord).filter(item => ['awaiting_stop', 'awaiting_assignment'].includes(text(item.stage)))
      .map(item => ({ reclaimId: text(item.reclaimId), taskId: text(item.taskId), taskRevision: Number(item.taskRevision), dispatchId: text(item.dispatchId), targetParticipantId: text(item.targetParticipantId), stage: item.stage as 'awaiting_stop' | 'awaiting_assignment' })),
    events: list(data.events).map(jevRecord).map(event => ({ id: text(event.source_id), kind: text(event.kind), state: text(event.state), result: resultObject(event.result_json) })),
    final: text(final.content) ? { content: text(final.content), status: text(final.status), evidence: strings(final.evidenceRefs) } : null,
    modelCards: list(data.modelCards).map(jevRecord),
    planApproval: ['planning', 'awaiting_input', 'awaiting_approval', 'deferred', 'approved'].includes(text(plan.status)) ? {
      status: plan.status as JevPlanApproval['status'], planHash: text(plan.planHash), requirementsRevision: Number(plan.requirementsRevision) || 0,
      lastActionClientMessageId: text(plan.lastActionClientMessageId),
      tasks: list(proposal.tasks).map(jevRecord).map(task => ({ key: text(task.key), objective: text(task.objective), expectedOutput: text(task.expectedOutput),
        acceptanceCriteria: strings(task.acceptanceCriteria), dependsOn: strings(task.dependsOn), contextRefs: strings(task.contextRefs), writeTargets: strings(task.writeTargets), ownerParticipantId: text(task.ownerParticipantId) })),
      clarifications: list(plan.clarifications ?? proposal.questions).map(jevRecord).map(question => ({ id: text(question.id), question: text(question.question), options: strings(question.options) })),
    } : null,
  };
}

export type JevTaskStage = 'queued' | 'dispatching' | 'dispatched' | 'running' | 'planning' | 'verifying' | 'synthesizing' | 'submitted' | 'review' | 'returned' | 'blocked' | 'unknown' | 'reclaiming' | 'reassigning' | 'revising' | 'superseded' | 'done' | 'failed' | 'cancelled';
export const JEV_TASK_STAGE_LABELS: Record<JevTaskStage, string> & Record<string, string> = {
  revising: '修改中 · 等待旧执行停止', superseded: '已由新版本接手', done: '已验收', failed: '未完成', cancelled: '已停止', reclaiming: '回收中 · 等待停止', reassigning: '已停止 · 等待改派', queued: '待调度', review: '待复核', returned: '待返修', blocked: '等待依赖', unknown: '回执待核实', submitted: '已提交', dispatched: '已接收', dispatching: '等待派发', running: '执行中', planning: '规划中', verifying: '复核中', synthesizing: '汇总中' };
export const jevTaskEffect = (task: JevTask, graph: JevSnapshot) => graph.effects.find(item => item.operation === 'dispatch'
  && item.request.taskId === task.id && item.request.taskRevision === task.revision);
export function jevTaskStage(task: JevTask, graph: JevSnapshot): JevTaskStage {
  if (graph.activeTaskIds && !graph.activeTaskIds.includes(task.id)) return 'superseded';
  if (graph.revisions?.some(item => item.status === 'awaiting_drain' && item.affectedTaskIds.includes(task.id))) return 'revising';
  if (task.state === 'done' || task.state === 'failed' || task.state === 'cancelled') return task.state;
  const reclaim = graph.reclaims?.find(item => item.taskId === task.id && item.taskRevision === task.revision && item.dispatchId === task.acceptedTurnId);
  if (reclaim) return reclaim.stage === 'awaiting_assignment' ? 'reassigning' : 'reclaiming';
  // A dispatch receipt confirms admission only. Runtime's running frontier is
  // the sole evidence for showing execution, even when WorkItem says active.
  const effect = jevTaskEffect(task, graph);
  if (effect?.executionStatus === 'running') return effect.request.purpose === 'plan' ? 'planning'
    : effect.request.purpose === 'verify' ? 'verifying' : effect.request.purpose === 'synthesize' ? 'synthesizing' : 'running';
  if (graph.running.includes(task.id)) return 'running';
  if (graph.review.includes(task.id) || task.state === 'review') return 'review';
  if (effect?.executionStatus === 'drained') return 'submitted';
  if (effect?.state === 'unknown') return 'unknown';
  if (effect?.state === 'accepted') return 'dispatched';
  if (effect && ['pending', 'sending'].includes(effect.state)) return 'dispatching';
  const returned = graph.events.some(event => {
    const application = jevRecord(jevRecord(event.result.receipt).application);
    const returnedTask = jevRecord(application.task);
    return application.status === 'applied' && application.operation === 'return'
      && returnedTask.id === task.id && returnedTask.revision === task.revision;
  });
  if (returned && task.state === 'queued') return 'returned';
  if (task.state === 'blocked' || graph.blocked.some(item => item.taskId === task.id)) return 'blocked';
  return task.state === 'active' ? 'unknown' : 'queued';
}
export const jevAwaitingPlan = (graph: JevSnapshot | null) => Boolean(graph && !graph.final && !graph.stopped
  && (['awaiting_input', 'awaiting_approval', 'deferred'].includes(graph.phase)
    || ['awaiting_input', 'awaiting_approval', 'deferred'].includes(graph.planApproval?.status || '')));
// Busy reserves the open Root for queueing/Stop, independently of motion.
export const jevIsBusy = (graph: JevSnapshot | null) => Boolean(graph && !graph.final && ((!graph.stopped && !jevAwaitingPlan(graph))
  || graph.running.length || graph.effects.some(effect => ['running', 'unknown'].includes(effect.executionStatus))));
export function jevAbstention(graph: JevSnapshot | null): JevSnapshot['events'][number] | null {
  if (!graph || graph.final || graph.stopped || jevAwaitingPlan(graph) || graph.running.length) return null;
  // The server returns owner events newest first. An older abstention cannot
  // override a later decision, queued event, or unresolved Runtime receipt.
  const latest = graph.events[0];
  if (latest?.state !== 'done' || latest.result.status !== 'abstained'
    || graph.events.some(event => ['pending', 'processing', 'retry', 'reconcile'].includes(event.state))
    || graph.effects.some(effect => ['prepared', 'admitted', 'running', 'unknown'].includes(effect.executionStatus)
      || ['pending', 'sending', 'unknown'].includes(effect.state)
      || effect.state === 'accepted' && !effect.executionStatus)) return null;
  return latest;
}
export const jevLeafTasks = (graph: JevSnapshot | null) => graph?.tasks.filter(task => !graph.tasks.some(child => child.parentId === task.id)
  && !(jevPhaseStep(graph.phase) === 'plan' && !task.parentId)) ?? [];
export function jevTaskCountLabel(graph: JevSnapshot | null) {
  if (jevAwaitingPlan(graph)) return graph?.planApproval?.tasks.length ? `${graph.planApproval.tasks.length} 项拟分工 · 未开始` : '正在明确目标与范围';
  if (graph?.phase === 'plan') return '正在形成执行方案';
  return `${jevLeafTasks(graph).length} 项执行任务`;
}
export function jevStatusLabel(graph: JevSnapshot | null, loading = false) {
  if (!graph) return loading ? '正在同步任务' : '从一个目标开始';
  if (graph.stopped) return jevIsBusy(graph) ? '正在停止，等待执行回执' : '已停止';
  if (graph.final) return graph.final.status === 'completed' ? '结果已汇总' : '本次未完成';
  const approvalStatus = graph.planApproval?.status || graph.phase;
  if (jevAwaitingPlan(graph)) return approvalStatus === 'awaiting_input' ? '等待补充目标与范围'
    : approvalStatus === 'deferred' ? '方案已保留，暂未执行' : '方案已就绪，等待确认';
  if (jevAbstention(graph)) return '暂未选出下一步';
  return ({ route: '判断任务路径', plan: '规划任务与依赖', awaiting_input: '等待补充目标与范围', awaiting_approval: '方案已就绪，等待确认', deferred: '方案已保留，暂未执行', execute: '推进任务与复核', synthesize: '汇总结果与证据', final: '等待最终答复同步' })[graph.phase];
}

type CreateInput = { message: string; strategy?: JevStrategy; attachmentIds?: string[]; previousRootId?: string; modelRouting?: JevModelRouting; toolApprovalMode?: JevToolApproval; verificationMode?: JevVerificationMode; executionApproval?: boolean };
type PendingCreate = { signature: string; clientMessageId: string; input: CreateInput; uncertain: boolean };
// A transport-scoped admission key survives Home -> Room and mode changes.
// Failed/uncertain requests keep their key; an explicit retry never creates a
// second root. There is no automatic command replay during GET or reconnect.
const pending = new WeakMap<ControlTransport, Map<string, PendingCreate>>();
function pendingKey(transport: ControlTransport, roomId: string) {
  const identity = transport.connectionIdentity || (transport.kind === 'native' ? 'native-local' : '');
  return identity ? `paw.jev.pending.v1:${encodeURIComponent(identity)}:${encodeURIComponent(roomId)}` : '';
}
function pendingEntry(transport: ControlTransport, roomId: string): PendingCreate | undefined {
  const cached = pending.get(transport)?.get(roomId);
  if (cached) return cached;
  const key = pendingKey(transport, roomId);
  if (!key) return;
  try {
    const data = jevRecord(JSON.parse(sessionStorage.getItem(key) || 'null'));
    if (typeof data.signature === 'string' && typeof data.clientMessageId === 'string' && typeof jevRecord(data.input).message === 'string') return data as PendingCreate;
  } catch { /* A disabled browser store does not invent an accepted request. */ }
}
function storePending(transport: ControlTransport, roomId: string, value?: PendingCreate) {
  let journal = pending.get(transport); if (!journal) { journal = new Map(); pending.set(transport, journal); }
  if (value) journal.set(roomId, value); else journal.delete(roomId);
  const key = pendingKey(transport, roomId);
  // A remount cannot keep observing this process's in-flight promise. Treat
  // the durable record as uncertain until a matching server receipt arrives.
  if (key) try { if (value) sessionStorage.setItem(key, JSON.stringify({ ...value, uncertain: true })); else sessionStorage.removeItem(key); } catch { /* In-memory admission identity still survives view changes. */ }
}
export const pendingJevInput = (transport: ControlTransport, roomId: string) => pendingEntry(transport, roomId)?.input;
export const uncertainJevInput = (transport: ControlTransport, roomId: string) => {
  const attempt = pendingEntry(transport, roomId);
  return attempt?.uncertain ? attempt.input : undefined;
};
export function acknowledgeJevAdmission(transport: ControlTransport, roomId: string, items: JevGraphItem[]) {
  const attempt = pendingEntry(transport, roomId);
  if (!attempt?.uncertain || !items.some(item => item.clientMessageId === attempt.clientMessageId)) return false;
  storePending(transport, roomId); return true;
}
export async function createJevWork(transport: ControlTransport, roomId: string, input: CreateInput) {
  const inputSignature = (value: CreateInput) => JSON.stringify({ message: value.message, strategy: value.strategy ?? 'auto', attachmentIds: value.attachmentIds ?? [], modelRouting: value.modelRouting ?? 'balanced', toolApprovalMode: value.toolApprovalMode ?? 'dispatch', verificationMode: value.verificationMode ?? 'auto', executionApproval: value.executionApproval ?? false });
  const signature = inputSignature(input);
  const previous = pendingEntry(transport, roomId);
  if (previous && inputSignature(previous.input) !== signature) throw new Error('上次发送尚未确认。请先用原内容重试，核实后再发送新任务。');
  const attempt = previous ?? { signature, clientMessageId: `paw-jev-${crypto.randomUUID()}`, input, uncertain: false };
  storePending(transport, roomId, attempt);
  try {
    const result = jevRecord(await observeRequest<ControlRequest, unknown>(request => transport.request(request), { pathId: 'agent.jev.command', params: { roomId }, timeoutMs: 30_000, body: {
      action: 'create', ...attempt.input, strategy: attempt.input.strategy ?? 'auto', clientMessageId: attempt.clientMessageId,
    } }, { timeout: 'Jev 发送超时，尚未确认接收。可同步状态或重试同一次发送。', aborted: 'Jev 发送观察已取消，尚未确认接收。' }));
    if (result.ok !== true || result.accepted !== true || !text(result.graphId)) throw new Error('服务端未确认 Jev 任务已接收，请使用原内容重试。');
    storePending(transport, roomId);
    return { ...result, graphId: text(result.graphId), rootId: text(result.rootId), clientMessageId: attempt.clientMessageId };
  } catch (reason) {
    const status = jevRecord(reason).status;
    // A first request rejected before admission can be corrected. A retry of
    // an earlier unknown outcome still needs reconciliation, even after 4xx.
    const rejected = !attempt.uncertain && typeof status === 'number' && [400, 401, 403, 404, 413, 422].includes(status);
    storePending(transport, roomId, rejected ? undefined : { ...attempt, uncertain: true });
    throw reason;
  }
}

export type JevPlanAction = 'approve_plan' | 'adjust_plan' | 'defer_plan';
export type JevPlanCommand = { action: JevPlanAction; graphId: string; rootId: string; planHash: string; message?: string; attachmentIds?: string[] };
type PendingPlan = { input: JevPlanCommand; clientMessageId: string; uncertain: boolean };
const planPending = new WeakMap<ControlTransport, Map<string, PendingPlan>>();
const planPendingKey = (transport: ControlTransport, roomId: string) => pendingKey(transport, roomId).replace('pending.v1:', 'plan.v1:');
function planEntry(transport: ControlTransport, roomId: string): PendingPlan | undefined {
  const cached = planPending.get(transport)?.get(roomId); if (cached) return cached;
  const key = planPendingKey(transport, roomId); if (!key) return;
  try {
    const value = jevRecord(JSON.parse(sessionStorage.getItem(key) || 'null'));
    const input = jevRecord(value.input);
    if (typeof value.clientMessageId === 'string' && ['approve_plan', 'adjust_plan', 'defer_plan'].includes(text(input.action))
      && text(input.graphId) && text(input.rootId) && typeof input.planHash === 'string') return value as PendingPlan;
  } catch { /* Unknown delivery remains recoverable in this view. */ }
}
function storePlan(transport: ControlTransport, roomId: string, value?: PendingPlan) {
  let journal = planPending.get(transport); if (!journal) { journal = new Map(); planPending.set(transport, journal); }
  if (value) journal.set(roomId, value); else journal.delete(roomId);
  const key = planPendingKey(transport, roomId);
  if (key) try { if (value) sessionStorage.setItem(key, JSON.stringify({ ...value, uncertain: true })); else sessionStorage.removeItem(key); } catch { /* The live journal still preserves the exact plan binding. */ }
}
export const uncertainJevPlan = (transport: ControlTransport, roomId: string) => {
  const entry = planEntry(transport, roomId); return entry?.uncertain ? entry.input : undefined;
};
export function acknowledgeJevPlan(transport: ControlTransport, roomId: string, graph: JevSnapshot | null) {
  const entry = planEntry(transport, roomId);
  if (!entry?.uncertain || graph?.graphId !== entry.input.graphId || graph.planApproval?.lastActionClientMessageId !== entry.clientMessageId) return;
  storePlan(transport, roomId); return entry.input;
}
export async function commandJevPlan(transport: ControlTransport, roomId: string, input: JevPlanCommand) {
  const previous = planEntry(transport, roomId);
  if (previous && JSON.stringify(previous.input) !== JSON.stringify(input)) throw new Error('上次方案操作尚未确认。请先核实同一次操作。');
  const attempt = previous ?? { input, clientMessageId: `paw-jev-plan-${crypto.randomUUID()}`, uncertain: false };
  storePlan(transport, roomId, attempt);
  try {
    const result = jevRecord(await observeRequest<ControlRequest, unknown>(request => transport.request(request), {
      pathId: 'agent.jev.command', params: { roomId }, timeoutMs: 30_000, body: { ...attempt.input, clientMessageId: attempt.clientMessageId },
    }, { timeout: '方案操作尚未确认，可核实同一次操作。' }));
    if (result.ok !== true || result.graphId !== input.graphId) throw new Error('未收到匹配的方案回执，请核实同一次操作。');
    storePlan(transport, roomId); return result;
  } catch (reason) {
    const status = jevRecord(reason).status;
    const rejected = !attempt.uncertain && typeof status === 'number' && [400, 401, 403, 404, 409, 413, 422].includes(status);
    storePlan(transport, roomId, rejected ? undefined : { ...attempt, uncertain: true }); throw reason;
  }
}
