import type { RoomSummary } from '@/features/rooms/room-types';
import type { RoomProjectionState } from '@/contracts/room-reducer';
import type { JevEffect, JevPlanTask, JevSnapshot, JevTask } from '@/features/semantic-workspace/jev-execution';
import type { JevMissionTask } from '@/features/semantic-workspace/jev-mission';

/** Read-only display data. The existing mission projection still owns task stages. */
export type CollaborationPerson = { id: string; name: string; ordinal: number; available: boolean };
export type CollaborationRun = {
  id: string; taskId: string; purpose: string; person?: CollaborationPerson;
  state: string; admitted: boolean; sessionId: string; model: string; latest: string;
};
export type CollaborationNode = {
  id: string; revision: number; index: number; title: string; objective: string;
  stage: string; label: string; tone: string; owner?: CollaborationPerson;
  task?: JevTask; proposal?: JevPlanTask; runs: CollaborationRun[];
  waitingOn: string[]; reasons: string[]; expected: string; acceptance: string[];
  result: string; refs: string[]; plan: boolean;
};
export type CollaborationEdge = { id: string; from: string; to: string; kind: string; context: boolean };
export type CollaborationModel = {
  graphId: string; revision: number; objective: string; nodes: CollaborationNode[];
  edges: CollaborationEdge[]; people: CollaborationPerson[]; runs: CollaborationRun[];
  counts: { accepted: number; running: number; reviewing: number; waiting: number; attention: number };
  notices: string[]; planned: boolean; stopped: boolean; final: boolean;
};
export type CollaborationLabels = Readonly<Record<string, string>>;
const IN_FLIGHT = new Set(['prepared', 'admitted', 'running', 'unknown']);
const ACTIVE_STAGES = new Set(['running', 'planning', 'synthesizing']);
const REVIEW_STAGES = new Set(['review', 'verifying', 'submitted']);
const ATTENTION_STAGES = new Set(['returned', 'unknown', 'failed']);
const WAIT_STAGES = new Set(['queued', 'blocked', 'dispatching', 'dispatched', 'reassigning', 'reclaiming', 'revising']);
export const collaborationText = (value: unknown): string => typeof value === 'string' ? value : '';
const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
export const shortCollaborationText = (value: string, max = 46): string => {
  const text = value.replace(/\s+/gu, ' ').trim();
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
};
export const isCurrentCollaborationRun = (run: CollaborationRun): boolean => IN_FLIGHT.has(run.state);
const PURPOSE_LABELS: Readonly<Record<string, string>> = { plan: '规划', execute: '执行', verify: '复核', synthesize: '汇总' };
export const collaborationPurpose = (purpose: string): string => PURPOSE_LABELS[purpose] ?? (purpose || '未标明用途');
const RUN_LABELS: Readonly<Record<string, string>> = { prepared: '待派发', admitted: '已接收', running: '进行中', unknown: '待核实', drained: '执行结束', completed: '执行结束', failed: '执行失败', cancelled: '已停止', aborted: '已停止' };
export const collaborationRunLabel = (state: string): string => RUN_LABELS[state] ?? (state || '待核实');

function admittedModel(effect: JevEffect): string {
  if (effect.state !== 'accepted' && effect.receipt.state !== 'accepted') return '';
  const selection = record(record(record(effect.request.contextManifest).executionScope).modelSelection);
  return [collaborationText(selection.modelId), collaborationText(selection.thinkingLevel)].filter(Boolean).join(' · ');
}
/** Missing optional scope comes from the enclosing snapshot; contradictory scope is rejected. */
function belongsToGraph(effect: JevEffect, graph: JevSnapshot, room?: RoomSummary): boolean {
  if (effect.operation !== 'dispatch') return false;
  for (const [key, expected] of [['graphId', graph.graphId], ['rootId', graph.rootId], ['roomId', room?.id || graph.roomId]] as const) {
    if (effect.request[key] !== undefined && expected && effect.request[key] !== expected) return false;
  }
  return graph.tasks.some(task => task.id === effect.request.taskId && task.revision === effect.request.taskRevision);
}
/** Index the public root once rather than scanning its history for every partner. */
function latestPublicActions(effects: JevEffect[], graph: JevSnapshot, projection?: RoomProjectionState): Map<string, string> {
  const result = new Map<string, { sequence: number; text: string }>();
  if (!projection) return new Map();
  const dispatches = new Map<string, string[]>(), turns = new Map<string, string[]>();
  const key = (owner: unknown, session: unknown, id: unknown) => JSON.stringify([owner, session, id]);
  for (const effect of effects) {
    if (!IN_FLIGHT.has(effect.executionStatus)) continue;
    const owner = effect.request.ownerId, session = effect.request.sessionId;
    if (!owner || !session) continue;
    if (effect.request.dispatchId) {
      const token = key(owner, session, effect.request.dispatchId);
      dispatches.set(token, [...(dispatches.get(token) ?? []), effect.effectId]);
    }
    if (effect.receipt.turnId) {
      const token = key(owner, session, effect.receipt.turnId);
      turns.set(token, [...(turns.get(token) ?? []), effect.effectId]);
    }
  }
  for (const id of projection.activityOrder) {
    const item = projection.activitiesById[id];
    if (!item?.summary) continue;
    const root = collaborationText(item.payload.rootId) || projection.turnsById[item.turnId]?.rootId || item.turnId;
    if (root !== graph.rootId) continue;
    const dispatch = collaborationText(item.payload.dispatchId);
    const ids = dispatch ? dispatches.get(key(item.participantId, item.sourceSessionId, dispatch))
      : turns.get(key(item.participantId, item.sourceSessionId, item.payload.sourceTurnId));
    // Legacy/public snapshot activities may omit sequence. Keep their list
    // order as a tie-breaker without mixing timestamps into sequence numbers.
    const sequence = typeof item.sequence === 'number' && Number.isFinite(item.sequence) ? item.sequence : 0;
    for (const effectId of ids ?? []) {
      if (!result.has(effectId) || sequence >= result.get(effectId)!.sequence) result.set(effectId, { sequence, text: item.summary });
    }
  }
  return new Map([...result].map(([id, value]) => [id, value.text]));
}

/** Pure projection. Does not issue requests, guess peers, infer percentages or mutate the graph. */
export function buildJevCollaboration(
  graph: JevSnapshot | null,
  room: RoomSummary | undefined,
  mission: readonly JevMissionTask[],
  labels: CollaborationLabels,
  nameOf: (ordinal: number) => string,
  projection?: RoomProjectionState,
): CollaborationModel {
  const people = (room?.participants ?? []).map(person => ({
    id: person.id, name: nameOf(person.ordinal), ordinal: person.ordinal, available: person.status === 'active',
  })).sort((a, b) => a.ordinal - b.ordinal);
  const peopleById = new Map(people.map(person => [person.id, person]));
  const notices: string[] = [];
  const effectRows = graph?.effects.filter(effect => belongsToGraph(effect, graph, room)) ?? [];
  const latest = graph ? latestPublicActions(effectRows, graph, projection) : new Map<string, string>();
  const runs: CollaborationRun[] = effectRows.map(effect => ({
    id: effect.effectId, taskId: collaborationText(effect.request.taskId), purpose: collaborationText(effect.request.purpose),
    person: peopleById.get(collaborationText(effect.request.ownerId)), state: effect.executionStatus,
    admitted: effect.state === 'accepted' || effect.receipt.state === 'accepted',
    sessionId: collaborationText(effect.request.sessionId), model: admittedModel(effect), latest: latest.get(effect.effectId) ?? '',
  }));
  const planned = Boolean(graph?.planApproval && ['awaiting_input', 'awaiting_approval', 'deferred'].includes(graph.planApproval.status));
  const currentMission = mission.filter(item => item.stage !== 'superseded');
  const nodes: CollaborationNode[] = planned ? (graph?.planApproval?.tasks ?? []).map((task, index) => ({
    id: task.key, revision: graph!.requirementsRevision, index, title: shortCollaborationText(task.objective), objective: task.objective,
    stage: 'proposal', label: '方案中 · 未执行', tone: 'waiting', owner: peopleById.get(task.ownerParticipantId),
    proposal: task, runs: [], waitingOn: task.dependsOn, reasons: [], expected: task.expectedOutput,
    acceptance: task.acceptanceCriteria, result: '', refs: [], plan: true,
  })) : currentMission.map((item, index) => ({
    id: item.task.id, revision: item.task.revision, index, title: shortCollaborationText(item.task.objective), objective: item.task.objective,
    stage: item.stage, label: labels[item.stage] || item.stage, tone: item.tone, owner: peopleById.get(item.task.ownerId),
    task: item.task, runs: runs.filter(run => run.taskId === item.task.id),
    waitingOn: [...new Set(item.waitingOn.map(task => task.id))], reasons: item.reasons,
    expected: item.task.expectedOutput, acceptance: item.task.acceptance, result: item.task.result,
    refs: [...new Set([...item.task.artifacts, ...item.task.evidence])], plan: false,
  }));
  const allEdges = planned ? (graph?.planApproval?.tasks ?? []).flatMap(task => task.dependsOn.map(id => ({
    prerequisite: id, dependent: task.key, kind: 'requires',
  }))) : graph?.edges ?? [];
  const seen = new Set<string>();
  const edges = allEdges.flatMap(edge => {
    const id = JSON.stringify([edge.prerequisite, edge.dependent, edge.kind]);
    if (seen.has(id)) return [];
    seen.add(id);
    return [{ id, from: edge.prerequisite, to: edge.dependent, kind: edge.kind, context: edge.kind === 'context' }];
  });
  const known = new Set(nodes.map(node => node.id));
  const external = edges.filter(edge => !known.has(edge.from) || !known.has(edge.to));
  if (external.length) notices.push(`${external.length} 条关系包含未在本视图展示的任务，缺失节点不按已完成处理。`);
  if (runs.some(run => !run.person)) notices.push('部分派发的伙伴不在当前名单中，保留回执，不替换成其他伙伴。');
  if (graph?.historicalTasks?.length) notices.push(`${graph.historicalTasks.length} 项旧版本任务留在原任务栏；这里仅展示当前版本。`);
  return {
    graphId: graph?.graphId ?? '', revision: graph?.requirementsRevision ?? 0,
    objective: graph?.currentRootObjective || graph?.tasks.find(task => !task.parentId)?.objective || room?.description || room?.title || '协作目标',
    nodes, edges, people, runs, notices, planned, stopped: graph?.stopped ?? false, final: Boolean(graph?.final),
    counts: {
      accepted: nodes.filter(node => node.stage === 'done').length,
      running: nodes.filter(node => ACTIVE_STAGES.has(node.stage)).length,
      reviewing: nodes.filter(node => REVIEW_STAGES.has(node.stage)).length,
      waiting: nodes.filter(node => WAIT_STAGES.has(node.stage)).length,
      attention: nodes.filter(node => ATTENTION_STAGES.has(node.stage)).length,
    },
  };
}

export type CollaborationPosition = { id: string; x: number; y: number; depth: number; unresolved: boolean };
export type CollaborationLayout = { positions: CollaborationPosition[]; width: number; height: number; levels: number; unresolved: string[] };
export const COLLABORATION_NODE_WIDTH = 226;
export const COLLABORATION_NODE_HEIGHT = 124;
/** Stable topology layout. Context edges never create a scheduling barrier. Cycles remain visible. */
export function layoutJevCollaboration(model: CollaborationModel): CollaborationLayout {
  const byId = new Map(model.nodes.map((node, index) => [node.id, index]));
  const degree = new Map(model.nodes.map(node => [node.id, 0]));
  const outgoing = new Map<string, string[]>();
  for (const edge of model.edges) {
    if (edge.context || !byId.has(edge.from) || !byId.has(edge.to)) continue;
    degree.set(edge.to, (degree.get(edge.to) ?? 0) + 1);
    outgoing.set(edge.from, [...(outgoing.get(edge.from) ?? []), edge.to]);
  }
  const queue = model.nodes.filter(node => degree.get(node.id) === 0).map(node => node.id);
  const depth = new Map<string, number>();
  for (const id of queue) depth.set(id, 0);
  for (let i = 0; i < queue.length; i++) {
    const id = queue[i]!;
    for (const next of outgoing.get(id) ?? []) {
      depth.set(next, Math.max(depth.get(next) ?? 0, (depth.get(id) ?? 0) + 1));
      degree.set(next, degree.get(next)! - 1);
      if (degree.get(next) === 0) queue.push(next);
    }
  }
  const visited = new Set(queue);
  const unresolved = model.nodes.filter(node => !visited.has(node.id)).map(node => node.id);
  const maxKnown = Math.max(0, ...queue.map(id => depth.get(id) ?? 0));
  for (const id of unresolved) depth.set(id, maxKnown + 1);
  const levels = model.nodes.length ? Math.max(0, ...depth.values()) + 1 : 0;
  const buckets = Array.from({ length: levels }, (_, level) => model.nodes.filter(node => depth.get(node.id) === level));
  const maxRows = Math.max(1, ...buckets.map(bucket => bucket.length));
  const height = 72 + maxRows * (COLLABORATION_NODE_HEIGHT + 28);
  const positions = buckets.flatMap((bucket, level) => bucket.map((node, row) => ({
    id: node.id, depth: level, x: 24 + level * (COLLABORATION_NODE_WIDTH + 64),
    y: 48 + (maxRows - bucket.length) * (COLLABORATION_NODE_HEIGHT + 28) / 2 + row * (COLLABORATION_NODE_HEIGHT + 28),
    unresolved: unresolved.includes(node.id),
  })));
  return { positions, width: Math.max(350, levels * (COLLABORATION_NODE_WIDTH + 64) - 16), height, levels, unresolved };
}

/** Connections are directional dependencies, not a claim that agents exchange messages. */
export function collaborationNeighborhood(model: CollaborationModel, selected: string): Set<string> {
  const connected = new Set<string>(selected ? [selected] : []);
  for (const reverse of [false, true]) {
    const visited = new Set<string>();
    const queue = selected ? [selected] : [];
    for (let i = 0; i < queue.length; i++) {
      const id = queue[i]!;
      if (visited.has(id)) continue;
      visited.add(id);
      for (const edge of model.edges) {
        if (edge.context || (reverse ? edge.to : edge.from) !== id) continue;
        const next = reverse ? edge.from : edge.to;
        connected.add(next);
        if (!visited.has(next)) queue.push(next);
      }
    }
  }
  return connected;
}
