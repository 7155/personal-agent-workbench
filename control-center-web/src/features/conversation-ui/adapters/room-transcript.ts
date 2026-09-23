import { approvalNeedsHumanDecision } from '@/contracts/approval-decision';
import { isModelAuthError, publicAgentErrorText } from '@/features/agent/public-error';
import type {
  RoomActivityProjection,
  RoomMessageProjection,
  RoomProjectionState,
  RoomTurnProjection,
} from '@/contracts/room-reducer';
import {
  roomDispatchPlanFromActivity,
  roomDispatchPlanSummary,
  roomDispatchSourceParticipantId,
  roomToolActivityLine,
  roomToolEvidence,
  type RoomDispatchPlan,
} from '@/paw-os/apps/room-gravity-projection';
import type {
  AssistantBlock,
  AssistantMessage,
  RunPhase,
  SteerReceiptState,
  ToolStatus,
  TranscriptMessage,
} from '../model/types';

export interface RoomTranscriptOptions {
  /** Reader-facing actor name for a Room participant (Sol, Mars, …). */
  actorName(participantId: string | null | undefined): string;
  /** Secondary actor line — collaboration role, when the Room knows one. */
  actorRole?(participantId: string | null | undefined): string;
  /** What a dispatched WorkItem is actually for, when the Room knows it. */
  workItemObjective?(workItemId: string): string;
  /** Restrict the transcript to one partner's public lane (satellite view). */
  participantId?: string;
}

export interface RoomTranscript {
  messages: TranscriptMessage[];
  /** Runtime activity behind a block, so a host can keep approvals and
   *  background-process links inside the shared card. */
  activityByBlockId: Record<string, RoomActivityProjection>;
  phase: RunPhase;
}

const EMPTY_TRANSCRIPT: RoomTranscript = { messages: [], activityByBlockId: {}, phase: 'idle' };

const RESOLVED_APPROVAL_STATES = ['approved', 'rejected', 'applied', 'resolved', 'cancelled'];

/** The state words `roomToolActivityLine` appends to a derived tool headline. */
const TOOL_STATE_WORDS = ['正在执行', '执行失败', '已停止', '已完成'];

/**
 * Project the authoritative Room reducer state onto the shared conversation
 * model: one Runtime loop becomes one assistant card, and that loop's public
 * reasoning, routing and tool work become blocks inside it.
 *
 * Folding happens in presentation only — no event is dropped or reordered,
 * and every block keeps a link back to its Runtime activity.
 */
export function roomTranscript(
  projection: RoomProjectionState | undefined,
  options: RoomTranscriptOptions,
): RoomTranscript {
  if (!projection) return EMPTY_TRANSCRIPT;

  const messages: TranscriptMessage[] = [];
  const activityByBlockId: Record<string, RoomActivityProjection> = {};
  const cardByKey = new Map<string, AssistantMessage>();
  let openKey = '';
  /* Every routing decision in the projection, so a child dispatch can still
   * name the planet whose gravity pulled it (its parent's target). */
  const dispatchPlans = projection.activityOrder
    .map((activityId) => projection.activitiesById[activityId])
    .map((activity) => activity ? roomDispatchPlanFromActivity(activity) : undefined)
    .filter((plan): plan is RoomDispatchPlan => Boolean(plan));

  const cardFor = (
    turnId: string,
    participantId: string | null,
    timestamp: number,
  ): AssistantMessage => {
    const key = `${turnId || 'room:ungrouped'}|${participantId ?? 'sol'}`;
    const existing = key === openKey ? cardByKey.get(key) : undefined;
    if (existing) return existing;
    const card: AssistantMessage = {
      id: `loop:${key}:${messages.length}`,
      role: 'assistant',
      timestamp,
      blocks: [],
      actor: options.actorName(participantId),
      turnId,
      ...(options.actorRole?.(participantId) ? { actorRole: options.actorRole(participantId) } : {}),
    };
    cardByKey.set(key, card);
    openKey = key;
    messages.push(card);
    return card;
  };

  for (const entry of roomChronology(projection, options.participantId)) {
    if (entry.kind === 'message' && entry.message.role === 'user') {
      openKey = '';
      const steerReceipt = roomSteerReceipt(entry.message, projection);
      messages.push({
        id: entry.message.id,
        role: 'user',
        text: entry.message.text,
        timestamp: entry.message.createdAtMs,
        deliveryStatus: userDeliveryStatus(entry.message),
        ...(steerReceipt ? { steerReceipt } : {}),
      });
      continue;
    }
    if (entry.kind === 'message') {
      const card = cardFor(entry.message.turnId, entry.message.participantId, entry.message.createdAtMs);
      const authError = entry.message.status === 'failed'
        ? entry.message.message?.blocks.find((block) => block.type === 'error'
          && block.visibility !== 'private_session' && isModelAuthError(block.data.message))
        : undefined;
      const authGuidance = authError ? publicAgentErrorText(authError.data.message) : '';
      if (authGuidance) card.error = authGuidance;
      card.blocks.push({
        id: `text:${entry.message.id}`,
        kind: 'text',
        text: authGuidance && entry.message.text.startsWith('模型服务未能生成最终回复')
          ? authGuidance
          : entry.message.text,
        ...(entry.message.status === 'streaming' ? { streaming: true } : {}),
      });
      continue;
    }
    if (entry.kind === 'activity') {
      const card = cardFor(entry.activity.turnId, entry.activity.participantId, entry.activity.createdAtMs);
      const block = activityBlock(entry.activity, dispatchPlans, options);
      card.blocks.push(block);
      activityByBlockId[block.id] = entry.activity;
      continue;
    }
    const card = cardFor(entry.turn.id, entry.turn.participantIds[0] ?? null, entry.turn.updatedAtMs || entry.turn.createdAtMs);
    card.error ||= entry.turn.status === 'aborted'
      ? entry.turn.failure || '这轮协作已停止。'
      : entry.turn.failure || '这轮协作未完成。';
    openKey = '';
  }

  return { messages, activityByBlockId, phase: roomPhase(projection) };
}

/** Which turn a failed/aborted card can safely retry from, if any. */
export function roomTranscriptRetrySource(
  projection: RoomProjectionState,
  turnId: string,
): { rootId: string; text: string } | undefined {
  const turn = projection.turnsById[turnId];
  if (!turn || roomTurnSupersededByUserInput(turn, projection)) return undefined;
  const source = turn.messageIds
    .map((messageId) => projection.messagesById[messageId])
    .find((message) => message?.role === 'user' && message.text.trim());
  if (!source) return undefined;
  return { rootId: turn.rootId || turn.id, text: source.text };
}

export function roomPhase(projection: RoomProjectionState | undefined): RunPhase {
  if (!projection) return 'idle';
  for (let index = projection.turnOrder.length - 1; index >= 0; index -= 1) {
    const status = projection.turnsById[projection.turnOrder[index] ?? '']?.status;
    if (status === 'queued') return 'sending';
    if (status === 'running') return 'responding';
  }
  return 'idle';
}

/** Approval blocks stay visible and undecided until Runtime resolves them. */
export function roomApprovalDecision(activity: RoomActivityProjection): {
  approvalId: string;
  payloadSha256: string;
} | undefined {
  const approvalId = text(activity.payload.approvalId);
  const payloadSha256 = text(activity.payload.payloadSha256);
  const resolutionState = text(activity.payload.resolutionState || activity.payload.state);
  if (!approvalId || !payloadSha256) return undefined;
  if (!approvalNeedsHumanDecision(activity.payload)) return undefined;
  if (RESOLVED_APPROVAL_STATES.includes(resolutionState)) return undefined;
  return { approvalId, payloadSha256 };
}

/* --------------------------------------------------------------------------- */

type RoomChronologyEntry =
  | { kind: 'message'; message: RoomMessageProjection; order: number; id: string }
  | { kind: 'activity'; activity: RoomActivityProjection; order: number; id: string }
  | { kind: 'terminal'; turn: RoomTurnProjection; order: number; id: string };

function roomChronology(
  projection: RoomProjectionState,
  participantId?: string,
): RoomChronologyEntry[] {
  const entries: RoomChronologyEntry[] = [];
  for (const messageId of projection.messageOrder) {
    const message = projection.messagesById[messageId];
    if (!message || message.projectionKind === 'execution') continue;
    if (!message.text.trim() && !message.question) continue;
    if (participantId && !messageBelongsToParticipant(message, participantId, projection)) continue;
    entries.push({
      kind: 'message',
      message,
      order: message.sequence ?? message.createdAtMs,
      id: `message:${message.id}`,
    });
  }
  for (const activityId of projection.activityOrder) {
    const activity = projection.activitiesById[activityId];
    if (!activity || !activityVisible(activity)) continue;
    if (participantId && !activityBelongsToParticipant(activity, participantId)) continue;
    entries.push({
      kind: 'activity',
      activity,
      order: activity.sequence ?? activity.createdAtMs,
      id: `activity:${activity.id}`,
    });
  }
  if (!participantId) {
    for (const turnId of projection.turnOrder) {
      const turn = projection.turnsById[turnId];
      if (!turn || (turn.status !== 'failed' && turn.status !== 'aborted')) continue;
      entries.push({
        kind: 'terminal',
        turn,
        order: (turn.updatedAtMs || turn.createdAtMs) + 0.75,
        id: `terminal:${turn.id}`,
      });
    }
  }
  return entries.sort((left, right) => left.order - right.order || left.id.localeCompare(right.id));
}

function messageBelongsToParticipant(
  message: RoomMessageProjection,
  participantId: string,
  projection: RoomProjectionState,
): boolean {
  if (message.participantId === participantId) return true;
  if (message.mentionedParticipantIds?.includes(participantId)) return true;
  if (!message.answerToPostId) return false;
  return projection.messagesById[message.answerToPostId]?.participantId === participantId;
}

function activityBelongsToParticipant(activity: RoomActivityProjection, participantId: string): boolean {
  const target = text(activity.payload.targetParticipantId);
  return target ? target === participantId : activity.participantId === participantId;
}

const PUBLIC_ACTIVITY_SIGNALS = ['reasoning', 'progress', 'route', 'route_decision', 'dispatch', 'intercom', 'status'];

function activitySignalSupported(signal: string): boolean {
  return signal === 'tool'
    || signal === 'work'
    || signal.startsWith('tool_')
    || signal.includes('route')
    || signal.includes('dispatch')
    || PUBLIC_ACTIVITY_SIGNALS.includes(signal);
}

function activityVisible(activity: RoomActivityProjection): boolean {
  if (text(activity.payload.approvalId)) return true;
  const declared = [text(activity.payload.sourceEventType), text(activity.payload.activityKind)]
    .map((signal) => signal.trim())
    .filter(Boolean);
  /* A payload that names its own event decides: when every declared signal is
     unsupported the entry is a machine event, and the generic reducer `kind`
     it arrived under must not smuggle it into a reader's transcript. */
  if (declared.length && !declared.some(activitySignalSupported)) return false;
  return [...declared, activity.kind].some(activitySignalSupported);
}

function activityBlock(
  activity: RoomActivityProjection,
  dispatchPlans: RoomDispatchPlan[],
  options: RoomTranscriptOptions,
): AssistantBlock {
  const eventType = text(activity.payload.sourceEventType, activity.kind);
  if (roomApprovalDecision(activity) || (text(activity.payload.approvalId) && !(eventType === 'tool' || eventType.startsWith('tool_')))) {
    return {
      id: `approval:${activity.id}`,
      kind: 'tool',
      name: '受控操作审批',
      summary: compact(activity.summary) || '等待你确认这项受控操作',
      status: roomApprovalDecision(activity) ? 'pending' : toolStatus(activity.status),
      startedAt: activity.createdAtMs,
    };
  }
  const plan = roomDispatchPlanFromActivity(activity);
  if (plan) {
    /* A route decision reads as the real dispatch — which planet pulled which,
     * and for what — never a dead「分派」label. */
    const sourceName = roomPublicPlanetName(
      options.actorName(roomDispatchSourceParticipantId(plan, dispatchPlans) || null),
    );
    const targetName = roomPublicPlanetName(options.actorName(plan.targetParticipantId));
    const objective = plan.workItemId ? options.workItemObjective?.(plan.workItemId) ?? '' : '';
    const routingDetail = [
      objective ? `任务：${objective}` : '',
      plan.routingPolicyLabel,
      ...plan.candidates.map((candidate) => (
        `${roomPublicPlanetName(options.actorName(candidate.participantId))} · ${candidate.score.toFixed(1)}${candidate.selected ? ' · 已选择' : ''}${candidate.signals.length ? ` · ${candidate.signals.join('、')}` : ''}`
      )),
      plan.dispatchId ? `分派 ${plan.dispatchId}` : '',
      plan.workItemId ? `任务 ${plan.workItemId}` : '',
    ].filter(Boolean).join('\n');
    /* Runtime's own line stays first: the derived plan summary explains the
     * routing, it does not replace what the Room actually published. A real
     * WorkItem objective is a paragraph, so it rides in the card body rather
     * than flooding the head. */
    const dispatchLine = [compact(activity.summary), roomDispatchPlanSummary(plan)]
      .filter(Boolean)
      .filter((part, index, parts) => parts.indexOf(part) === index);
    return {
      id: `dispatch:${activity.id}`,
      kind: 'tool',
      name: `${sourceName} → ${targetName} · 任务分派`,
      summary: dispatchLine.join(' · '),
      status: toolStatus(activity.status),
      ...(routingDetail ? { output: routingDetail } : {}),
      startedAt: activity.createdAtMs,
    };
  }
  if (eventType === 'tool' || eventType.startsWith('tool_')) {
    const evidence = roomToolEvidence(activity.payload);
    /* Runtime often echoes a raw argument blob as the summary. A reader line
     * is derived from real evidence instead; the blob stays reachable as the
     * card's input, so folding never costs a trace. */
    const raw = rawDetail(activity.summary);
    const name = evidence?.label || '工具';
    const line = roomToolActivityLine(raw ? '' : activity.summary, activity.payload, activity.status);
    /* The card head already names the tool and carries its state, so a derived
     * line of exactly those two would print the same sentence twice. One that
     * carries the real op (`行星协调 · 批量并行委派`) still says something. */
    const derived = roomToolActivityLine('', activity.payload, activity.status);
    const duplicate = line === derived
      && TOOL_STATE_WORDS.some((word) => derived === `${name} ${word}`);
    return {
      id: `tool:${activity.id}`,
      kind: 'tool',
      name,
      summary: duplicate ? '' : line,
      status: toolStatus(activity.status),
      ...(raw ? { input: activity.summary.trim() } : {}),
      ...(evidence?.facts.length
        ? { output: evidence.facts.map((fact) => `${fact.label}：${fact.value}`).join('\n') }
        : {}),
      startedAt: activity.createdAtMs,
    };
  }
  if (text(activity.payload.activityKind) === 'work') {
    const receipt = roomWorkActivityReceipt(activity, options);
    return {
      id: `note:${activity.id}`,
      kind: 'thinking',
      summary: receipt.summary,
      status: activity.status === 'running' || activity.status === 'waiting' ? 'running' : 'done',
      ...(receipt.detail ? { detail: receipt.detail } : {}),
      startedAt: activity.createdAtMs,
    };
  }
  return {
    id: `note:${activity.id}`,
    kind: 'thinking',
    summary: compact(activity.summary) || (activity.kind === 'reasoning' ? '正在形成可公开的思考摘要' : '公开进展已更新'),
    status: activity.status === 'running' || activity.status === 'waiting' ? 'running' : 'done',
    ...(rawDetail(activity.summary) ? { detail: activity.summary.trim() } : {}),
    startedAt: activity.createdAtMs,
  };
}

function roomWorkActivityReceipt(
  activity: RoomActivityProjection,
  options: RoomTranscriptOptions,
): { summary: string; detail: string } {
  const payload = activity.payload;
  const phase = text(payload.phase);
  const titleByPhase: Readonly<Record<string, string>> = {
    assigned: '任务已分派',
    assignment_failed: '任务分派失败',
    submitted: '已提交验收',
    completed: '任务已完成',
    returned: '需求已更新',
    retried: '需求已更新',
    reassigned: '负责人变更',
    blocked: '任务已阻塞',
    escalated: '任务已升级处理',
    failed: '任务未完成',
    aborted: '任务已停止',
  };
  const title = titleByPhase[phase] || '任务状态已更新';
  const previousRevision = positiveInteger(payload.previousWorkItemRevision);
  const currentRevision = positiveInteger(payload.currentWorkItemRevision)
    || positiveInteger(payload.workItemRevision);
  const revision = previousRevision && currentRevision && previousRevision !== currentRevision
    ? `r${previousRevision}→r${currentRevision}`
    : currentRevision
      ? `r${currentRevision}`
      : '';
  const ownerParticipantId = text(payload.ownerParticipantId);
  const owner = ownerParticipantId
    ? roomPublicPlanetName(options.actorName(ownerParticipantId))
    : '';
  return {
    summary: [title, revision, owner ? `负责人 ${owner}` : ''].filter(Boolean).join(' · '),
    detail: [
      text(payload.workItemId) ? `任务 ${text(payload.workItemId)}` : '',
      text(payload.reason) ? `原因 ${text(payload.reason)}` : '',
      text(payload.documentRef) ? `文档 ${text(payload.documentRef)}` : '',
    ].filter(Boolean).join('\n'),
  };
}

function positiveInteger(value: unknown): number {
  return typeof value === 'number' && Number.isInteger(value) && value > 0 ? value : 0;
}

/** Public Room dispatch copy is planet-only. Runtime display names are kept in
 * the parsed plan for internal correlation, but they must never become a
 * reader-facing fallback when a participant alias is unavailable. */
function roomPublicPlanetName(value: string | undefined): string {
  const name = value?.trim();
  return name || '协作行星';
}

function toolStatus(status: RoomActivityProjection['status']): ToolStatus {
  if (status === 'running' || status === 'waiting') return 'running';
  if (status === 'failed') return 'error';
  if (status === 'aborted') return 'cancelled';
  return 'success';
}

const NON_TERMINAL_TURN_STATES = ['queued', 'running'];

/**
 * A message written into a collaboration that is already in flight is a steer,
 * and a steer is the one place a plain timestamp lies: it says when the writer
 * sent it, never whether anybody has it yet.
 *
 * Every state below is read from the authoritative projection. `unread` is the
 * reducer's own optimistic flag — the post exists only in this client, so no
 * partner can have seen it. Once Runtime publishes it the post carries the
 * running Root's turn, and from there the turn's own events answer the rest:
 * nothing after it yet is `read`, published work after it is `settling`, and a
 * terminal Root is `done`. A message that opens its own Root is not a steer and
 * keeps the ordinary timestamp.
 */
function roomSteerReceipt(
  message: RoomMessageProjection,
  projection: RoomProjectionState,
): SteerReceiptState | undefined {
  if (message.projectionKind === 'optimistic') {
    const runInFlight = projection.turnOrder.some((turnId) => (
      turnId !== message.turnId
      && NON_TERMINAL_TURN_STATES.includes(projection.turnsById[turnId]?.status ?? '')
    ));
    return runInFlight ? 'unread' : undefined;
  }
  const turn = projection.turnsById[message.turnId];
  if (!turn) return undefined;
  const at = chronologyOrder(message);
  if (!turnWorkAround(turn, projection, message.id, (order) => order < at)) return undefined;
  if (!NON_TERMINAL_TURN_STATES.includes(turn.status)) return 'done';
  return turnWorkAround(turn, projection, message.id, (order) => order > at) ? 'settling' : 'read';
}

/** Whether the Root published any partner work on the given side of a post. */
function turnWorkAround(
  turn: RoomTurnProjection,
  projection: RoomProjectionState,
  exceptMessageId: string,
  side: (order: number) => boolean,
): boolean {
  const activity = turn.activityIds.some((activityId) => {
    const entry = projection.activitiesById[activityId];
    return entry ? side(chronologyOrder(entry)) : false;
  });
  if (activity) return true;
  return turn.messageIds.some((messageId) => {
    const entry = projection.messagesById[messageId];
    return Boolean(
      entry && entry.id !== exceptMessageId && entry.role === 'assistant' && side(chronologyOrder(entry)),
    );
  });
}

/** The same order key `roomChronology` sorts on: server sequence when the
 *  publication carries one, wall clock only as the optimistic fallback. */
function chronologyOrder(entry: { sequence?: number; createdAtMs: number }): number {
  return entry.sequence ?? entry.createdAtMs;
}

function userDeliveryStatus(message: RoomMessageProjection): 'sending' | 'sent' | 'failed' {
  if (message.status === 'failed' || message.status === 'aborted') return 'failed';
  if (message.status === 'queued' && message.projectionKind === 'optimistic') return 'sending';
  return 'sent';
}

function roomTurnSupersededByUserInput(
  turn: RoomTurnProjection,
  projection: RoomProjectionState,
): boolean {
  const indexes = turn.messageIds
    .map((messageId) => projection.messageOrder.indexOf(messageId))
    .filter((index) => index >= 0);
  const boundary = indexes.length ? Math.max(...indexes) : -1;
  if (boundary >= 0) {
    return projection.messageOrder.slice(boundary + 1).some((messageId) => {
      const message = projection.messagesById[messageId];
      return message?.role === 'user' && message.text.trim().length > 0;
    });
  }
  const turnIndex = projection.turnOrder.indexOf(turn.id);
  if (turnIndex < 0) return false;
  return projection.turnOrder.slice(turnIndex + 1).some((turnId) => (
    projection.turnsById[turnId]?.messageIds.some((messageId) => {
      const message = projection.messagesById[messageId];
      return message?.role === 'user' && message.text.trim().length > 0;
    }) ?? false
  ));
}

function rawDetail(value: string): boolean {
  const source = value.trim();
  return source.includes('\n')
    || /```|(?:^|\s)[{[]\s*["']/u.test(source)
    || source.length > 180;
}

function compact(value: string): string {
  const source = value.replace(/\s+/gu, ' ').trim();
  if (!source) return '';
  if (/^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/iu.test(source)) return '';
  return source.length > 180 ? `${source.slice(0, 177).trimEnd()}…` : source;
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}
