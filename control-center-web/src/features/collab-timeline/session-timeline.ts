import type { AgentSubagentRunV1 } from '@/contracts/generated/agent-subagent-run.v1';
import { subagentPresentationState } from '@/features/agent/status/subagent-presentation';
import {
  collabPhasesFromEvidence,
  type CollabEvent,
  type CollabHandoff,
  type CollabLane,
  type CollabMark,
  type CollabSegment,
  type CollabTimeline,
} from './model';

/**
 * One Agent Session and its Tool Agents ("卫星"). The Session is the origin;
 * every retained run node is a lane below the run that launched it. Retries
 * are attempts of the same node, drawn as consecutive segments on one lane.
 */
export const SESSION_LANE = 'session';

const TEMPLATE: Record<AgentSubagentRunV1['templateId'], string> = {
  researcher: '研究员', planner: '规划员', worker: '执行者', reviewer: '审阅者', delegate: '委派者',
};
const KIND: Record<AgentSubagentRunV1['templateId'], CollabSegment['kind']> = {
  researcher: 'execute', planner: 'plan', worker: 'execute', reviewer: 'review', delegate: 'plan',
};

export function buildSessionCollabTimeline(input: {
  sessionId: string;
  title: string;
  runs: readonly AgentSubagentRunV1[];
  busy?: boolean;
  nowMs?: number;
}): CollabTimeline {
  const nowMs = input.nowMs ?? Date.now();
  const runs = [...input.runs].sort((a, b) => (a.startedAtMs ?? a.createdAtMs) - (b.startedAtMs ?? b.createdAtMs));
  const byNode = new Map<string, AgentSubagentRunV1[]>();
  for (const run of runs) {
    const key = run.nodeId || run.id;
    byNode.set(key, [...(byNode.get(key) ?? []), run]);
  }
  const nodeOfRun = new Map(runs.map((run) => [run.id, run.nodeId || run.id]));
  const live = runs.some((run) => run.state === 'queued' || run.state === 'running');
  const startMs = Math.min(nowMs, ...runs.map((run) => run.createdAtMs));
  const endMs = live ? nowMs : Math.max(startMs + 1, ...runs.map((run) => run.completedAtMs ?? run.updatedAtMs));

  const lanes: CollabLane[] = [{
    id: SESSION_LANE, kind: 'origin', label: input.title || '当前 Session', role: 'Pi Session · 主持',
    depth: 0, state: input.busy ? 'working' : 'idle', status: input.busy ? '执行中' : live ? '有子任务运行' : '子任务已结束',
  }];
  const segments: CollabSegment[] = [];
  const handoffs: CollabHandoff[] = [];
  const marks: CollabMark[] = [];
  const events: CollabEvent[] = [];
  const reached = new Set<string>();
  let failed = 0;
  let returned = 0;
  const visited = new Set<string>();

  // Depth-first so children sit directly below their parent.
  const roots = [...byNode.entries()].filter(([, attempts]) => !attempts[0]!.parentRunId || !nodeOfRun.has(attempts[0]!.parentRunId));
  const visit = (nodeId: string, attempts: AgentSubagentRunV1[], parentLane: string) => {
    if (visited.has(nodeId)) return;
    visited.add(nodeId);
    const latest = attempts.reduce((a, b) => (b.attemptNumber >= a.attemptNumber ? b : a));
    const state = subagentPresentationState(latest);
    const running = latest.state === 'queued' || latest.state === 'running';
    const bad = ['failed', 'timed_out', 'aborted', 'contract_invalid'].includes(state);
    if (latest.state === 'completed' && !bad) returned += 1;
    const laneId = `run:${nodeId}`;
    lanes.push({
      id: laneId, kind: 'agent', label: TEMPLATE[latest.templateId], parentId: parentLane,
      role: short(latest.todoTask || latest.task, 26) || '子 Agent', depth: Math.max(1, latest.depth),
      state: running ? latest.state === 'queued' ? 'waiting' : KIND[latest.templateId] === 'review' ? 'reviewing' : KIND[latest.templateId] === 'plan' ? 'thinking' : 'working' : bad ? 'error' : 'done',
      status: running ? latest.state === 'queued' ? '排队中' : '运行中' : state === 'aborted' ? '已停止' : bad ? '失败' : '已返回，待主对话确认',
      sessionId: latest.childSessionId,
      runId: latest.id,
      ...(latest.launchDigest.modelProfile ? { model: [latest.launchDigest.modelProfile, latest.launchDigest.thinkingLevel].filter(Boolean).join(' · ') } : {}),
      joinedAtMs: attempts[0]!.createdAtMs,
    });
    for (const run of attempts) {
      const from = run.startedAtMs ?? run.createdAtMs;
      const isRunning = run.state === 'queued' || run.state === 'running';
      const to = isRunning ? endMs : Math.max(from + 1, run.completedAtMs ?? run.updatedAtMs);
      const runBad = ['failed', 'timed_out', 'aborted', 'contract_invalid'].includes(subagentPresentationState(run));
      if (run.startedAtMs && run.startedAtMs > run.createdAtMs + 500) {
        segments.push({ id: `q:${run.id}`, laneId, kind: 'wait', startMs: run.createdAtMs, endMs: run.startedAtMs, open: false, label: '排队' });
      }
      segments.push({ id: `s:${run.id}`, laneId, kind: run.state === 'queued' ? 'wait' : KIND[run.templateId], startMs: from, endMs: to, open: isRunning, label: `${TEMPLATE[run.templateId]}${run.attemptNumber > 1 ? ` · 第 ${run.attemptNumber} 次` : ''}`, failed: runBad });
      if (run.startedAtMs) reached.add(KIND[run.templateId] === 'review' ? 'review' : KIND[run.templateId] === 'plan' ? 'plan' : 'execute');
      handoffs.push({ id: `l:${run.id}`, kind: run.attemptNumber > 1 ? 'return' : 'dispatch', fromLaneId: parentLane, toLaneId: laneId, atMs: run.createdAtMs, label: run.attemptNumber > 1 ? '重试' : run.launchDigest.contextMode === 'fork' ? 'Fork 派发' : '派发' });
      events.push({ id: `l:${run.id}`, laneId, atMs: run.createdAtMs, tone: 'satellite', actor: parentLane === SESSION_LANE ? (input.title || 'Session') : '上级卫星', text: `${run.attemptNumber > 1 ? '重试' : '派发'} ${TEMPLATE[run.templateId]}：${short(run.task, 40)}` });
      if (!isRunning) {
        handoffs.push({ id: `r:${run.id}`, kind: 'result', fromLaneId: laneId, toLaneId: parentLane, atMs: to, label: runBad ? '失败' : '返回', failed: runBad });
        events.push({ id: `r:${run.id}`, laneId, atMs: to, tone: runBad ? 'fail' : 'done', actor: TEMPLATE[run.templateId], text: runBad ? `失败${run.error ? `：${short(run.error, 40)}` : ''}` : `返回结果 · ${run.usage.toolCount} 次工具 · ${compact(run.usage.totalTokens)} Token` });
        if (runBad) failed += 1;
        if (run.contract.status === 'valid') {
          events.push({ id: `c:${run.id}`, laneId, atMs: run.contract.validatedAtMs ?? to,
            tone: 'satellite', actor: TEMPLATE[run.templateId], text: '输出格式检查通过；不代表任务验收' });
        } else if (run.contract.status === 'invalid') {
          marks.push({ id: `c:${run.id}`, laneId, kind: 'return', atMs: to, label: '输出合同无效' });
        }
      }
      // Aggregate tool counts have no individual timestamps; never place
      // a synthetic tool event halfway through a run.
    }
    const children = [...byNode.entries()].filter(([, items]) => items.some((item) => item.parentRunId && nodeOfRun.get(item.parentRunId) === nodeId));
    for (const [childId, childAttempts] of children) visit(childId, childAttempts, laneId);
  };
  for (const [nodeId, attempts] of roots) visit(nodeId, attempts, SESSION_LANE);
  // Partial/cyclic retained ancestry must not hide a run or recurse forever.
  for (const [nodeId, attempts] of byNode) if (!visited.has(nodeId)) visit(nodeId, attempts, SESSION_LANE);

  const firstLaunch = runs[0] ? runs[0].createdAtMs : startMs;
  const lastReturn = runs.length && !live ? Math.max(...runs.map((run) => run.completedAtMs ?? run.updatedAtMs)) : endMs;
  if (runs.length) {
    segments.push({ id: 's:session:wait', laneId: SESSION_LANE, kind: 'origin', startMs: firstLaunch, endMs: lastReturn, open: live, label: '子任务活动时段' });
    // Child completion alone says nothing about the parent's final reply.
  }
  segments.push({ id: 's:session', laneId: SESSION_LANE, kind: 'origin', startMs, endMs, open: live, label: 'Session' });

  events.sort((a, b) => a.atMs - b.atMs);
  handoffs.sort((a, b) => a.atMs - b.atMs);
  const running = lanes.filter((lane) => lane.kind === 'agent' && ['working', 'thinking', 'reviewing'].includes(lane.state)).length;
  const focus = [...handoffs].reverse().find((handoff) => !handoff.failed);
  return {
    id: `session:${input.sessionId}`,
    title: input.title || 'Session',
    lanes,
    segments,
    handoffs,
    marks,
    events,
    phases: collabPhasesFromEvidence({ reached, final: !live && runs.length > 0 }),
    startMs,
    endMs,
    live,
    final: false,
    scope: 'session',
    settled: !live && runs.length > 0,
    stopped: false,
    counts: { accepted: 0, returned, total: byNode.size, running, tools: runs.reduce((sum, run) => sum + run.usage.toolCount, 0), failed, satellites: byNode.size },
    ...(focus ? { focus: { laneId: focus.toLaneId, label: '子任务', state: live ? 'moving' : 'done' } } : {}),
  };
}

function short(value: string, limit: number): string {
  const clean = value.replace(/\s+/gu, ' ').trim();
  return clean.length > limit ? `${clean.slice(0, limit - 1)}…` : clean;
}
function compact(value: number): string {
  return value >= 10_000 ? `${(value / 1000).toFixed(0)}k` : value >= 1000 ? `${(value / 1000).toFixed(1)}k` : String(value);
}
