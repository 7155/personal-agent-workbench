import type { RoomActivityProjection } from '@/contracts/room-reducer';
import { roomActivityFlowKind } from '@/features/rooms/room-flow-projection';
import { roomParticipantPlanetName } from '@/features/rooms/room-participant-identity';

/**
 * Sol gravity projection — pure readers that turn raw Room routing and tool
 * payloads into legible dispatch plans, parallel waves and tool evidence.
 * Everything here is derived from real Runtime payloads (route_decision,
 * tool_started/tool_finished); nothing fabricates flow that did not happen.
 */

export interface RoomDispatchCandidate {
  participantId: string;
  displayName: string;
  score: number;
  signals: string[];
  selected: boolean;
}

/** One authoritative routing decision: who was chosen, why, and inside which
 * parallel wave. Parsed from the real `route_decision` payload. */
export interface RoomDispatchPlan {
  dispatchId: string;
  parentDispatchId: string;
  child: boolean;
  reason: string;
  reasonLabel: string;
  routingPolicy: string;
  routingPolicyLabel: string;
  targetParticipantId: string;
  targetDisplayName: string;
  waveId: string;
  phaseName: string;
  /** 0-based track index inside the wave; -1 when the decision is not parallel. */
  parallelIndex: number;
  parallelSize: number;
  workItemId: string;
  workItemState: string;
  candidates: RoomDispatchCandidate[];
}

export interface RoomDispatchWave {
  waveId: string;
  phaseName: string;
  parallelSize: number;
  dispatches: RoomDispatchPlan[];
}

const dispatchReasonLabels: Record<string, string> = {
  facilitator: '主持人开场接管',
  partner_delegate: '伙伴委派',
  moderator: '主持人指定',
  explicit_invite: '点名邀请',
  explicit_mention: '@ 点名',
  mention: '@ 点名',
  fallback: '回退到默认伙伴',
  round_robin: '轮流发言',
};

const routingPolicyLabels: Record<string, string> = {
  parallel: '并行协作',
  moderator: '主持路由',
  manual_mentions: '手动点名',
  sequential: '顺序发言',
  natural: '自然发言',
  invite_only: '邀请制',
};

export function roomDispatchReasonLabel(reason: string): string {
  return dispatchReasonLabels[reason] ?? (reason ? `按「${reason}」选择` : '按路由策略选择');
}

export function roomRoutingPolicyLabel(policy: string): string {
  return routingPolicyLabels[policy] ?? policy;
}

/** Parse a dispatch plan from a route/dispatch activity. Returns undefined for
 * every non-dispatch activity so callers can fall through to plain rendering. */
export function roomDispatchPlanFromActivity(activity: RoomActivityProjection): RoomDispatchPlan | undefined {
  if (roomActivityFlowKind(activity) !== 'dispatch') return undefined;
  return roomDispatchPlanFromPayload(activity.payload);
}

export function roomDispatchPlanFromPayload(payload: Record<string, unknown>): RoomDispatchPlan | undefined {
  const targetParticipantId = stringValue(payload.targetParticipantId)
    || stringArrayValue(payload.selectedParticipantIds).at(0)
    || '';
  const dispatchId = stringValue(payload.dispatchId || payload.childDispatchId);
  if (!targetParticipantId && !dispatchId) return undefined;
  const reason = stringValue(payload.reason);
  const routingPolicy = stringValue(payload.routingPolicy);
  const selectedIds = new Set([
    ...stringArrayValue(payload.selectedParticipantIds),
    ...(targetParticipantId ? [targetParticipantId] : []),
  ]);
  const candidates = candidateArray(payload.candidates)
    .map((candidate) => ({ ...candidate, selected: selectedIds.has(candidate.participantId) }));
  return {
    dispatchId,
    parentDispatchId: stringValue(payload.parentDispatchId),
    child: payload.child === true,
    reason,
    reasonLabel: roomDispatchReasonLabel(reason),
    routingPolicy,
    routingPolicyLabel: roomRoutingPolicyLabel(routingPolicy),
    targetParticipantId,
    targetDisplayName: stringValue(payload.targetDisplayName),
    waveId: stringValue(payload.waveId),
    phaseName: stringValue(payload.phaseName),
    parallelIndex: numberValue(payload.parallelIndex, -1),
    parallelSize: numberValue(payload.parallelSize, 0),
    workItemId: stringValue(payload.workItemId),
    workItemState: stringValue(payload.workItemState),
    candidates,
  };
}

/** Every dispatch plan in real event order, ready for wave grouping and for
 * resolving who issued a child dispatch (target of the parent dispatch). */
export function roomDispatchPlans(activities: RoomActivityProjection[]): RoomDispatchPlan[] {
  return activities
    .map((activity) => roomDispatchPlanFromActivity(activity))
    .filter((plan): plan is RoomDispatchPlan => Boolean(plan));
}

/** The planet that exerted the gravity: a child dispatch was issued by the
 * target of its parent dispatch; a root dispatch comes from Sol (''). */
export function roomDispatchSourceParticipantId(
  plan: RoomDispatchPlan,
  plans: RoomDispatchPlan[],
): string {
  if (!plan.parentDispatchId) return '';
  return plans.find((candidate) => candidate.dispatchId === plan.parentDispatchId)?.targetParticipantId ?? '';
}

/** Group parallel dispatches into their waves, preserving track order. Only
 * real waves (waveId present) group; solitary decisions stay out. */
export function roomDispatchWaves(plans: RoomDispatchPlan[]): RoomDispatchWave[] {
  const waves = new Map<string, RoomDispatchWave>();
  for (const plan of plans) {
    if (!plan.waveId) continue;
    const wave = waves.get(plan.waveId) ?? {
      waveId: plan.waveId,
      phaseName: plan.phaseName,
      parallelSize: plan.parallelSize,
      dispatches: [],
    };
    if (!wave.phaseName && plan.phaseName) wave.phaseName = plan.phaseName;
    wave.parallelSize = Math.max(wave.parallelSize, plan.parallelSize, wave.dispatches.length + 1);
    if (!wave.dispatches.some((existing) => existing.dispatchId === plan.dispatchId)) wave.dispatches.push(plan);
    waves.set(plan.waveId, wave);
  }
  for (const wave of waves.values()) {
    wave.dispatches.sort((left, right) => (
      (left.parallelIndex < 0 ? Number.MAX_SAFE_INTEGER : left.parallelIndex)
      - (right.parallelIndex < 0 ? Number.MAX_SAFE_INTEGER : right.parallelIndex)
    ) || left.dispatchId.localeCompare(right.dispatchId));
  }
  return [...waves.values()];
}

/** One human line for a dispatch: reason, then wave track, then phase. The
 * metaphor never replaces task text — callers append the WorkItem objective. */
export function roomDispatchPlanSummary(plan: RoomDispatchPlan, targetName?: string): string {
  const parts = [
    `${plan.reasonLabel}${targetName ? ` · 交给 ${targetName}` : ''}`,
  ];
  if (plan.parallelIndex >= 0 && plan.parallelSize > 1) parts.push(`并行轨道 ${plan.parallelIndex + 1}/${plan.parallelSize}`);
  if (plan.phaseName) parts.push(plan.phaseName);
  return parts.join(' · ');
}

/* ---------------------------------------------------------------------------
 * Room tool naming and evidence. Raw tool ids like `room_partner` / `agents`
 * are Runtime identifiers, not reader-facing copy.
 */

const roomToolLabels: Record<string, string> = {
  room_partner: '行星协调',
  agents: '子 Agent 编排',
  agent_goal: '目标看板',
  workspace_job: '后台任务',
  skill_load: '加载 Skill',
  skill_search: '检索 Skill',
  tool_search: '检索工具',
  tool_load: '加载工具',
  bash: '终端命令',
  read: '读取文件',
  write: '写入文件',
  edit: '编辑文件',
  ls: '浏览目录',
  find: '查找文件',
  grep: '搜索内容',
  browser: '浏览器操作',
  desktop_semantic: '桌面检查',
  runtime: '运行时',
};

/** Op labels are scoped to the owning tool: `status` means partner status only
 * on `room_partner`; on `workspace_job` or `browser` it is that tool's own op
 * and must stay the honest machine word instead of a wrong translation. */
const roomToolOpLabels: Record<string, Record<string, string>> = {
  room_partner: {
    list: '查看伙伴名册',
    delegate: '委派任务',
    delegate_batch: '批量并行委派',
    message: '给伙伴留言',
    status: '查看伙伴状态',
  },
};

export function roomGravityToolLabel(toolName: string): string {
  const key = toolName.trim();
  return roomToolLabels[key] ?? (key || '工具');
}

export function roomToolOpLabel(toolName: string, op: string): string {
  return roomToolOpLabels[toolName.trim()]?.[op] ?? op;
}

export interface RoomToolFact {
  label: string;
  value: string;
}

/** Structured, human-readable evidence for one tool activity: what the tool
 * was, what it was asked, and what it returned. Used by satellite disclosure. */
export interface RoomToolEvidence {
  toolName: string;
  label: string;
  headline: string;
  facts: RoomToolFact[];
}

export function roomToolEvidence(payload: Record<string, unknown>): RoomToolEvidence | undefined {
  const toolName = stringValue(payload.toolName || payload.toolId);
  if (!toolName) return undefined;
  const label = roomGravityToolLabel(toolName);
  const args = recordValue(payload.arguments);
  const result = recordValue(payload.result);
  const op = stringValue(args.op);
  const facts: RoomToolFact[] = [];
  const error = stringValue(payload.error || result.error);
  if (error) facts.push({ label: '失败原因', value: compactText(error) });
  const output = stringValue(result.outputPreview);
  if (output && output !== error) facts.push({ label: '执行输出', value: output.slice(0, 2000) });
  if (op) facts.push({ label: '操作', value: roomToolOpLabel(toolName, op) });
  for (const [key, value] of Object.entries(args)) {
    if (key === 'op' || value == null) continue;
    const text = compactValue(value);
    if (text) facts.push({ label: key, value: text });
    if (facts.length >= 6) break;
  }
  const partners = candidateLikeArray(result.partners);
  if (partners.length) {
    facts.push({
      label: '伙伴',
      value: partners
        .map((partner) => [
          roomParticipantPlanetName({ ordinal: numberValue(partner.ordinal, -1) }),
          stringValue(partner.collaborationRole),
        ].filter(Boolean).join(' · '))
        .filter(Boolean)
        .join('；'),
    });
  }
  const operation = stringValue(result.operation);
  if (operation && operation !== op) facts.push({ label: '结果操作', value: roomToolOpLabel(toolName, operation) });
  const resultSummary = stringValue(result.summary || result.message || result.status);
  if (resultSummary) facts.push({ label: '结果', value: compactText(resultSummary) });
  const resolvedOp = op || operation;
  const headline = resolvedOp
    ? `${label} · ${roomToolOpLabel(toolName, resolvedOp)}`
    : label;
  return { toolName, label, headline, facts: facts.slice(0, 8) };
}

/** True when a tool activity summary is only the machine identifier the
 * Runtime echoed back (`agents`, `room_partner`, `skill_load`…) — never a
 * sentence a reader should see as the row text. */
export function roomToolSummaryIsMachine(summary: string, payload: Record<string, unknown>): boolean {
  const source = summary.trim();
  if (!source) return true;
  if (source === stringValue(payload.toolName) || source === stringValue(payload.toolId)) return true;
  return /^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/u.test(source);
}

/** The one reader-facing line for a tool activity. Real prose summaries pass
 * through untouched; a machine-id summary falls back to the tool's evidence
 * headline (label · real op) plus the honest execution state — the row is
 * never a bare Runtime id and never an empty「工具」. */
export function roomToolActivityLine(
  summary: string,
  payload: Record<string, unknown>,
  status: string,
): string {
  const result = recordValue(payload.result);
  const error = stringValue(payload.error || result.error);
  const source = summary.trim();
  if (source && !roomToolSummaryIsMachine(source, payload)) return source;
  if (status === 'failed' && error) return compactText(error);
  const headline = stringValue(payload.displayName)
    || roomToolEvidence(payload)?.headline
    || roomGravityToolLabel(stringValue(payload.toolName) || stringValue(payload.toolId));
  if (['queued', 'running', 'waiting', 'pending'].includes(status)) return `${headline} 正在执行`;
  if (status === 'failed') return `${headline} 执行失败`;
  if (['aborted', 'cancelled', 'stopped'].includes(status)) return `${headline} 已停止`;
  return `${headline} 已完成`;
}

/* --------------------------------------------------------------------------- */

function candidateArray(value: unknown): Omit<RoomDispatchCandidate, 'selected'>[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => recordValue(item))
    .map((item) => ({
      participantId: stringValue(item.participantId),
      displayName: stringValue(item.displayName),
      score: numberValue(item.score, 0),
      signals: stringArrayValue(item.signals),
    }))
    .filter((item) => item.participantId);
}

function candidateLikeArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map((item) => recordValue(item)) : [];
}

function compactValue(value: unknown): string {
  if (typeof value === 'string') return compactText(value);
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) return value.length ? `${value.length} 项` : '';
  if (typeof value === 'object' && value !== null) {
    const keys = Object.keys(value);
    return keys.length ? `${keys.length} 个字段` : '';
  }
  return '';
}

function compactText(value: string): string {
  const compact = value.replace(/\s+/gu, ' ').trim();
  return compact.length > 120 ? `${compact.slice(0, 117).trimEnd()}…` : compact;
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function stringArrayValue(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && Boolean(item.trim()))
    : [];
}

function numberValue(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function recordValue(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}
