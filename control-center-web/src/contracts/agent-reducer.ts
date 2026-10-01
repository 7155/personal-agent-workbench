import type { UiAgentEvent, UiAgentMessage } from './ui-events';
import type { AgentSessionTelemetryV1 } from './generated/agent-session-telemetry.v1';
import type { AgentBackgroundJobV1 } from './generated/agent-background-job.v1';
import type { AgentLifecycleCancellationAuditV1 } from './generated/agent-lifecycle-cancellation-audit.v1';
import type {
  ActGate as AgentActGateProjection,
  Goal as AgentGoalProjection,
  Todo as AgentTodoContract,
} from './generated/agent-workflow-state.v1';
import { parseAgentEvent, tryParseAgentMessage, validateContract } from './validators';
import { approvalDecisionView, approvalNeedsHumanDecision } from './approval-decision';

export type AgentTurnStatus =
  | 'queued'
  | 'running'
  | 'waiting'
  | 'completed'
  | 'failed'
  | 'aborted';

export interface AgentTurnProjection {
  id: string;
  status: AgentTurnStatus;
  messageIds: string[];
  activityIds: string[];
  createdAtMs: number;
  updatedAtMs: number;
  failure?: string;
}

export interface AgentActivityProjection {
  id: string;
  turnId: string;
  kind: string;
  status: 'running' | 'waiting' | 'completed' | 'failed';
  summary: string;
  payload: Record<string, unknown>;
  createdAtMs: number;
  updatedAtMs: number;
  /** First source-event sequence, used only as a stable timeline tie-breaker. */
  timelineSequence?: number;
  /** A turn ended before this activity supplied its own terminal receipt. */
  settledByTurnStatus?: 'completed' | 'failed' | 'aborted';
}

export type AgentMessageProjection = UiAgentMessage & {
  /** First source-event sequence; snapshots created before this field still sort by timestamp. */
  timelineSequence?: number;
};

export interface AgentToolProgressEntry {
  eventId: string;
  kind: 'tool_started' | 'tool_progress' | 'tool_finished';
  status: AgentActivityProjection['status'];
  summary: string;
  createdAtMs: number;
}

export interface ProjectionDiagnostic {
  id: string;
  streamKind: 'agent' | 'room';
  eventType: string;
  summary: string;
  sequence: number;
  payload: Record<string, unknown>;
}

export interface ProjectionGap {
  expectedSequence: number;
  receivedSequence: number;
  receivedEventId: string;
}

export interface AgentProjectionState {
  sessionId: string;
  lastSequence: number;
  lastEventId: string;
  resumeToken: string;
  needsSnapshot: boolean;
  gap?: ProjectionGap;
  status: string;
  messagesById: Record<string, AgentMessageProjection>;
  messageOrder: string[];
  turnsById: Record<string, AgentTurnProjection>;
  turnOrder: string[];
  activitiesById: Record<string, AgentActivityProjection>;
  activityOrder: string[];
  optimisticByClientMessageId: Record<string, string>;
  diagnostics: ProjectionDiagnostic[];
  telemetry?: AgentSessionTelemetryV1;
  messageQueue: AgentMessageQueue;
  todo: AgentTodoProjection;
  goal: AgentGoalProjection;
  actGate: AgentActGateProjection;
  backgroundJobsById: Record<string, AgentBackgroundJobV1>;
  backgroundJobOrder: string[];
  lifecycleCancellationAuditsById: Record<string, AgentLifecycleCancellationAuditV1>;
  lifecycleCancellationAuditOrder: string[];
}

export interface AgentMessageQueue {
  steering: string[];
  followUp: string[];
}

export type AgentTodoProjection = AgentTodoContract;

export type ProjectionDisposition =
  | 'applied'
  | 'ignored-duplicate'
  | 'ignored-foreign'
  | 'ignored-snapshot-pending'
  | 'snapshot-required';

export interface ProjectionReduction<State> {
  state: State;
  disposition: ProjectionDisposition;
}

export interface AgentSnapshot {
  messages: unknown[];
  liveEvents: unknown[];
  lastSequence: number;
  resumeToken: string;
  snapshotScope?: 'recent';
  partial?: boolean;
  runtimeQuiescent?: boolean;
  status?: string;
  telemetry?: unknown;
  messageQueue?: unknown;
  todo?: unknown;
  goal?: unknown;
  actGate?: unknown;
  backgroundJobs?: unknown;
  lifecycleCancellationAudits?: unknown;
}

export interface OptimisticAgentMessageInput {
  clientMessageId: string;
  retryOfClientMessageId?: string;
  text: string;
  attachments?: string[];
  nowMs: number;
  turnId?: string;
  delivery?: 'prompt' | 'steer' | 'followUp';
}

const diagnosticLimit = 50;

export function createAgentProjection(sessionId: string): AgentProjectionState {
  return {
    sessionId,
    lastSequence: 0,
    lastEventId: '',
    resumeToken: '',
    needsSnapshot: false,
    status: 'idle',
    messagesById: {},
    messageOrder: [],
    turnsById: {},
    turnOrder: [],
    activitiesById: {},
    activityOrder: [],
    optimisticByClientMessageId: {},
    diagnostics: [],
    telemetry: undefined,
    messageQueue: { steering: [], followUp: [] },
    todo: emptyAgentTodo(sessionId),
    goal: emptyAgentGoal(),
    actGate: closedActGate(),
    backgroundJobsById: {},
    backgroundJobOrder: [],
    lifecycleCancellationAuditsById: {},
    lifecycleCancellationAuditOrder: [],
  };
}

export function reduceAgentEvent(
  state: AgentProjectionState,
  event: UiAgentEvent,
): ProjectionReduction<AgentProjectionState> {
  if (event.sessionId !== state.sessionId) {
    return { state, disposition: 'ignored-foreign' };
  }
  if (event.eventType === 'snapshot_required') {
    return {
      state: {
        ...state,
        needsSnapshot: true,
        gap: {
          expectedSequence: state.lastSequence + 1,
          receivedSequence: event.sequence,
          receivedEventId: event.eventId,
        },
      },
      disposition: 'snapshot-required',
    };
  }
  if (event.sequence <= state.lastSequence) {
    return { state, disposition: 'ignored-duplicate' };
  }
  if (state.needsSnapshot && event.eventType !== 'snapshot') {
    return { state, disposition: 'ignored-snapshot-pending' };
  }
  if (
    event.eventType !== 'snapshot' &&
    state.lastSequence > 0 &&
    event.sequence !== state.lastSequence + 1
  ) {
    return {
      state: {
        ...state,
        needsSnapshot: true,
        gap: {
          expectedSequence: state.lastSequence + 1,
          receivedSequence: event.sequence,
          receivedEventId: event.eventId,
        },
      },
      disposition: 'snapshot-required',
    };
  }

  if (event.eventType === 'snapshot') {
    const snapshot = snapshotFromEvent(event, state.lastSequence);
    const replaced = applyAgentSnapshot(state, snapshot);
    return {
      state: {
        ...replaced,
        lastSequence: Math.max(event.sequence, snapshot.lastSequence),
        lastEventId: event.eventId,
        resumeToken: snapshot.resumeToken || event.resumeToken,
      },
      disposition: 'applied',
    };
  }

  let next = cloneState(state);
  next.lastSequence = event.sequence;
  next.lastEventId = event.eventId;
  next.resumeToken = event.resumeToken;
  const payload = record(event.payload);
  const telemetry = parseTelemetry(payload.telemetry);
  if (telemetry) next.telemetry = telemetry;

  switch (event.eventType) {
    case 'text_delta':
      applyTextDelta(next, event, payload);
      break;
    case 'message_completed':
      applyCompletedMessage(next, event, payload);
      break;
    case 'compaction_started':
      upsertCompactionActivity(next, event, payload, 'running');
      break;
    case 'compaction_completed':
      upsertCompactionActivity(next, event, payload, payload.error ? 'failed' : 'completed');
      break;
    case 'status_changed':
      if (text(payload.phase) === 'provider_retry') {
        const activityState = text(payload.activityState);
        const retryStatus: AgentActivityProjection['status'] =
          activityState === 'completed'
            ? 'completed'
            : activityState === 'failed'
              ? 'failed'
              : 'running';
        upsertActivity(next, event, payload, retryStatus);
      }
      next.status = text(payload.status) || next.status;
      touchTurn(next, event.turnId, turnStatusFromRuntime(next.status), event.createdAtMs);
      break;
    case 'message_queue_updated':
      {
        const previousQueue = next.messageQueue;
        next.messageQueue = parseMessageQueue(payload);
        reconcileMessageDeliveryQueue(next, previousQueue, next.messageQueue);
      }
      break;
    case 'workflow_changed':
      next.todo = parseAgentTodo(payload.todo) ?? next.todo;
      next.goal = parseAgentGoal(payload.goal) ?? next.goal;
      next.actGate = parseActGate(
        payload.actGate,
        next.todo.revision,
        next.goal.revision,
      ) ?? next.actGate;
      break;
    case 'lifecycle_cancellation_changed': {
      const audit = parseLifecycleCancellationAudit(payload.audit);
      if (audit && audit.sessionId === state.sessionId) upsertLifecycleCancellationAudit(next, audit);
      break;
    }
    case 'reasoning_summary': {
      if (text(payload.source) !== 'provider_reasoning_summary') break;
      const reasoningState = text(payload.state);
      const reasoningStatus: AgentActivityProjection['status'] = reasoningState === 'running'
        ? 'running'
        : reasoningState === 'failed'
          ? 'failed'
          : 'completed';
      upsertActivity(next, event, payload, reasoningStatus);
      if (reasoningStatus !== 'failed') {
        reopenProvisionalTurn(next, event.turnId, event.createdAtMs);
      }
      if (reasoningStatus === 'running') next.status = 'analyzing';
      break;
    }
    case 'tool_started':
    case 'tool_progress':
      // Delegation publishes detached lifecycle receipts so the dedicated
      // subagent projection can refresh while a child runs. They are not a
      // second parent Tool call: the real `agents` Tool already carries the
      // parent's turn/call ids and receives its own terminal event. Projecting
      // this synthetic receipt into the transcript creates an `unscoped` turn
      // that can never finish.
      if (
        event.eventType === 'tool_progress'
        && !event.turnId
        && text(payload.toolCallId).startsWith('subagent:')
      ) break;
      upsertActivity(next, event, payload, payload.isError === true ? 'failed' : 'running');
      if (payload.isError !== true) {
        reopenProvisionalTurn(next, event.turnId, event.createdAtMs);
      }
      next.status = payload.isError === true ? 'failed' : 'working';
      break;
    case 'tool_finished': {
      const correlatedPayload = mergeLegacyApprovalIntoTool(next, event, payload);
      const expectedNoop = expectedToolNoop(correlatedPayload);
      const approvalDecision = approvalDecisionView(correlatedPayload);
      const approvalState = text(correlatedPayload.state);
      const approvalDenied = (
        approvalDecision.status === 'failed_closed'
        || approvalDecision.decision === 'deny'
        || (
          Boolean(text(correlatedPayload.approvalId))
          && ['rejected', 'expired', 'stale', 'failed'].includes(approvalState)
        )
      );
      const projectedPayload = expectedNoop
        ? {
            ...correlatedPayload,
            isError: false,
            expectedNoop: true,
            ...(expectedNoop.kind === 'act_gate' ? { governanceBlocked: true } : {}),
            summary: expectedNoop.summary,
          }
        : correlatedPayload;
      upsertActivity(
        next,
        event,
        projectedPayload,
        expectedNoop || (correlatedPayload.isError !== true && !approvalDenied) ? 'completed' : 'failed',
      );
      break;
    }
    case 'approval_required': {
      const roomToolOwner = roomBoundApprovalToolOwner(next, payload);
      upsertApprovalActivity(next, event, payload, roomToolOwner?.status ?? 'waiting');
      if (roomToolOwner) {
        next.status = 'working';
        touchTurn(next, roomToolOwner.turnId, 'running', event.createdAtMs);
      } else {
        next.status = 'waiting';
        touchTurn(
          next,
          approvalActivityTurnId(next, payload, event.turnId),
          'waiting',
          event.createdAtMs,
        );
      }
      break;
    }
    case 'user_input_required': {
      const resolved = ['resolved', 'cancelled'].includes(text(payload.resolutionState));
      upsertActivity(next, event, payload, resolved ? 'completed' : 'waiting');
      if (!resolved) {
        next.status = 'waiting';
        touchTurn(next, event.turnId, 'waiting', event.createdAtMs);
      }
      break;
    }
    case 'approval_resolved': {
      const roomToolOwner = roomBoundApprovalToolOwner(next, payload);
      const resolutionState = text(payload.state);
      const roomAuthorizationApplied = Boolean(
        roomToolOwner
        && payload.automatic === true
        && text(payload.decisionMode) === 'policy'
        && ['approved', 'applied', 'external_pending'].includes(resolutionState),
      );
      upsertApprovalActivity(
        next,
        event,
        payload,
        roomAuthorizationApplied
          ? roomToolOwner!.status
          : ['approved', 'applied', 'external_pending'].includes(resolutionState)
            ? 'completed'
          : 'failed',
      );
      if (roomAuthorizationApplied) {
        next.status = 'working';
        touchTurn(next, roomToolOwner!.turnId, 'running', event.createdAtMs);
      }
      break;
    }
    case 'background_job_started':
    case 'background_job_progress':
    case 'background_job_completed':
    case 'background_job_failed':
    case 'background_job_cancelled': {
      const job = parseBackgroundJob(payload.job);
      if (job) upsertBackgroundJob(next, job);
      break;
    }
    case 'memory_checkpointed':
      // This is a derived source-capture receipt, not a new Agent run. The
      // owning Tool is already visible in its authoritative turn and Memory
      // maintenance has its own typed event/status surface. Projecting this
      // unscoped bookkeeping event used to create another completed avatar
      // underneath the still-running Tool card.
      break;
    case 'memory_maintenance_updated':
      upsertActivity(next, event, payload, 'completed');
      break;
    case 'session_configuration_changed':
      // Model/thinking state is refreshed from the authoritative Pi catalog
      // by AgentFeature; it is not a visible timeline activity.
      break;
    case 'turn_completed':
      completeTurn(
        next,
        event.turnId,
        payload.status === 'aborted' || payload.aborted === true ? 'aborted' : 'completed',
        event.createdAtMs,
      );
      next.status = 'idle';
      break;
    case 'turn_failed':
      if (!event.turnId) {
        appendDiagnostic(next, {
          id: event.eventId,
          streamKind: 'agent',
          eventType: 'turn_failed',
          summary: 'Ignored turn_failed without a turnId; no user turn was failed.',
          sequence: event.sequence,
          payload,
        });
        break;
      }
      completeTurn(next, event.turnId, 'failed', event.createdAtMs, text(payload.error));
      next.status = 'failed';
      upsertActivity(next, event, payload, 'failed');
      break;
    case 'unknown':
      appendDiagnostic(next, {
        id: event.eventId,
        streamKind: 'agent',
        eventType: event.rawEventType || 'unknown',
        summary: 'Unsupported agent event was retained for diagnostics.',
        sequence: event.sequence,
        payload,
      });
      break;
    case 'heartbeat':
      break;
  }
  return { state: next, disposition: 'applied' };
}

export function reduceAgentEvents(
  state: AgentProjectionState,
  events: readonly UiAgentEvent[],
): AgentProjectionState {
  let next = state;
  let index = 0;
  while (index < events.length) {
    const event = events[index];
    if (!event) break;
    if (!canStartAgentDeltaBatch(next, event)) {
      next = reduceAgentEvent(next, event).state;
      index += 1;
      continue;
    }

    let end = index + 1;
    let delta = text(event.payload.delta);
    let last = event;
    while (end < events.length) {
      const candidate = events[end];
      if (!candidate || !canMergeAgentDelta(last, candidate)) break;
      delta += text(candidate.payload.delta);
      last = candidate;
      end += 1;
    }

    const reduced = reduceAgentEvent(next, {
      ...event,
      payload: { ...event.payload, delta },
    });
    next = reduced.state;
    if (reduced.disposition === 'applied' && last !== event) {
      next = {
        ...next,
        lastSequence: last.sequence,
        lastEventId: last.eventId,
        resumeToken: last.resumeToken,
      };
    }
    index = end;
  }
  return next;
}

export function rewriteOptimisticAgentMessage(
  state: AgentProjectionState,
  targetMessageId: string,
  input: OptimisticAgentMessageInput,
): AgentProjectionState {
  const target = state.messagesById[targetMessageId];
  if (!target || target.role !== 'user' || targetMessageId.startsWith('local:')) {
    throw new TypeError('rewrite target must be a durable user message');
  }
  const targetMessageIndex = state.messageOrder.indexOf(targetMessageId);
  if (targetMessageIndex < 0) throw new TypeError('rewrite target is not in the visible branch');

  const next = cloneState(state);
  const targetTurnIndex = next.turnOrder.indexOf(target.turnId);
  const removedTurnIds = new Set(
    targetTurnIndex >= 0
      ? next.turnOrder.slice(targetTurnIndex)
      : [target.turnId],
  );
  const removedMessageIds = new Set(
    next.messageOrder.filter((messageId, index) => {
      const message = next.messagesById[messageId];
      return index >= targetMessageIndex || (message ? removedTurnIds.has(message.turnId) : false);
    }),
  );
  for (const messageId of removedMessageIds) delete next.messagesById[messageId];
  next.messageOrder = next.messageOrder.filter((messageId) => !removedMessageIds.has(messageId));

  const removedActivityIds = new Set(
    next.activityOrder.filter((activityId) => {
      const activity = next.activitiesById[activityId];
      return activity ? removedTurnIds.has(activity.turnId) : false;
    }),
  );
  for (const activityId of removedActivityIds) delete next.activitiesById[activityId];
  next.activityOrder = next.activityOrder.filter((activityId) => !removedActivityIds.has(activityId));
  for (const turnId of removedTurnIds) delete next.turnsById[turnId];
  next.turnOrder = next.turnOrder.filter((turnId) => !removedTurnIds.has(turnId));
  for (const [clientMessageId, messageId] of Object.entries(next.optimisticByClientMessageId)) {
    if (removedMessageIds.has(messageId)) delete next.optimisticByClientMessageId[clientMessageId];
  }
  next.messageQueue = { steering: [], followUp: [] };
  next.status = 'idle';
  return appendOptimisticAgentMessage(next, input);
}

export function appendOptimisticAgentMessage(
  state: AgentProjectionState,
  input: OptimisticAgentMessageInput,
): AgentProjectionState {
  if (!input.clientMessageId.trim()) throw new TypeError('clientMessageId must not be empty');
  if (state.optimisticByClientMessageId[input.clientMessageId]) return state;

  const next = cloneState(state);
  const messageId = `local:${input.clientMessageId}`;
  const turnId = input.turnId || `local-turn:${input.clientMessageId}`;
  const existingTurnStatus = next.turnsById[turnId]?.status;
  const message: UiAgentMessage = {
    schemaVersion: 'rag-ime.agent-message.v1',
    id: messageId,
    sessionId: state.sessionId,
    turnId,
    role: 'user',
    status: 'queued',
    blocks: [
      {
        id: `${messageId}:text`,
        type: 'text',
        status: 'completed',
        presentationKind: 'plain_text',
        data: {
          text: input.text,
          ...(input.delivery && input.delivery !== 'prompt' ? { delivery: input.delivery } : {}),
        },
      },
    ],
    attachments: [...(input.attachments ?? [])],
    citations: [],
    createdAtMs: input.nowMs,
    completedAtMs: null,
    clientMessageId: input.clientMessageId,
    ...(input.delivery && input.delivery !== 'prompt'
      ? { deliveryState: 'sending' as const }
      : {}),
    ...(input.retryOfClientMessageId
      ? { retryOfClientMessageId: input.retryOfClientMessageId }
      : {}),
  };
  next.messagesById[messageId] = message;
  next.messageOrder.push(messageId);
  next.optimisticByClientMessageId[input.clientMessageId] = messageId;
  attachMessageToTurn(next, message);
  if (!existingTurnStatus) touchTurn(next, turnId, 'queued', input.nowMs);
  next.status = 'busy';
  return next;
}

export function acknowledgeOptimisticAgentMessage(
  state: AgentProjectionState,
  clientMessageId: string,
  nowMs: number,
): AgentProjectionState {
  const messageId = state.optimisticByClientMessageId[clientMessageId]
    ?? state.messageOrder.find((id) => state.messagesById[id]?.clientMessageId === clientMessageId);
  if (!messageId) return state;
  const message = state.messagesById[messageId];
  if (!message || !messageDelivery(message) || message.deliveryState === 'applied') return state;
  const next = cloneState(state);
  next.messagesById[messageId] = {
    ...message,
    deliveryState: 'accepted',
    deliveryAcceptedAtMs: nowMs,
  };
  return next;
}

export function failOptimisticAgentMessage(
  state: AgentProjectionState,
  clientMessageId: string,
  error: string,
  nowMs: number,
  admissionState?: 'ambiguous' | 'pending' | 'unresolved',
): AgentProjectionState {
  const messageId = state.optimisticByClientMessageId[clientMessageId];
  if (!messageId) return state;
  const message = state.messagesById[messageId];
  if (!message) return state;
  const next = cloneState(state);
  if (admissionState === 'pending' || admissionState === 'unresolved') {
    next.messagesById[messageId] = {
      ...message,
      status: 'queued',
      completedAtMs: null,
      admissionState,
    };
    touchTurn(next, message.turnId, 'waiting', nowMs);
    delete next.turnsById[message.turnId]?.failure;
    next.status = 'busy';
    return next;
  }
  next.messagesById[messageId] = {
    ...message,
    status: 'failed',
    ...(admissionState ? { admissionState } : {}),
  };
  completeTurn(next, message.turnId, 'failed', nowMs, error);
  next.status = 'failed';
  return next;
}

export function requeueOptimisticAgentMessage(
  state: AgentProjectionState,
  clientMessageId: string,
  nowMs: number,
): AgentProjectionState {
  const messageId = state.optimisticByClientMessageId[clientMessageId];
  if (!messageId) return state;
  const message = state.messagesById[messageId];
  if (!message) return state;
  const next = cloneState(state);
  const requeued = {
    ...message,
    status: 'queued' as const,
    completedAtMs: null,
  };
  delete requeued.admissionState;
  next.messagesById[messageId] = requeued;
  touchTurn(next, message.turnId, 'queued', nowMs);
  delete next.turnsById[message.turnId]?.failure;
  next.status = 'busy';
  return next;
}

export function discardOptimisticAgentMessage(
  state: AgentProjectionState,
  clientMessageId: string,
): AgentProjectionState {
  const messageId = state.optimisticByClientMessageId[clientMessageId];
  if (!messageId) return state;
  const message = state.messagesById[messageId];
  if (!message) return state;

  const next = cloneState(state);
  delete next.optimisticByClientMessageId[clientMessageId];
  delete next.messagesById[messageId];
  next.messageOrder = next.messageOrder.filter((id) => id !== messageId);
  detachMessageFromTurn(next, message);
  const turn = next.turnsById[message.turnId];
  if (turn && turn.messageIds.length === 0 && turn.activityIds.length === 0) {
    delete next.turnsById[message.turnId];
    next.turnOrder = next.turnOrder.filter((id) => id !== message.turnId);
  }
  next.status = next.turnOrder.some((turnId) => {
    const status = next.turnsById[turnId]?.status;
    return status === 'queued' || status === 'running' || status === 'waiting';
  }) ? 'busy' : 'idle';
  return next;
}

export function abortAgentTurn(
  state: AgentProjectionState,
  turnId: string,
  nowMs: number,
): AgentProjectionState {
  if (!state.turnsById[turnId]) return state;
  const next = cloneState(state);
  completeTurn(next, turnId, 'aborted', nowMs);
  next.status = 'idle';
  return next;
}

/**
 * A recent snapshot is a bounded view, not a replacement transcript. Keep
 * durable messages already projected locally and let the recent response
 * overwrite only matching ids or append newer rows.
 */
function mergeBoundedRecentMessages(
  state: AgentProjectionState,
  recentMessages: readonly unknown[],
): unknown[] {
  const merged: unknown[] = [];
  const positions = new Map<string, number>();
  for (const messageId of state.messageOrder) {
    const message = state.messagesById[messageId];
    if (!message || message.id.startsWith('local:')) continue;
    positions.set(message.id, merged.length);
    merged.push(message);
  }
  for (const rawMessage of recentMessages) {
    const rawId = record(rawMessage).id;
    const id = typeof rawId === 'string' ? rawId : '';
    const existingIndex = id ? positions.get(id) : undefined;
    if (existingIndex !== undefined) {
      merged[existingIndex] = rawMessage;
      continue;
    }
    if (id) positions.set(id, merged.length);
    merged.push(rawMessage);
  }
  return merged;
}

function preserveConfirmedSnapshotActivities(
  current: AgentProjectionState,
  projection: AgentProjectionState,
): AgentProjectionState {
  const next = {
    ...projection,
    turnsById: { ...projection.turnsById },
    turnOrder: [...projection.turnOrder],
    activitiesById: { ...projection.activitiesById },
    activityOrder: [...projection.activityOrder],
  };
  for (const activityId of current.activityOrder) {
    const activity = current.activitiesById[activityId];
    if (!activity || next.activitiesById[activityId]) continue;
    next.activitiesById[activityId] = { ...activity };
    next.activityOrder.push(activityId);
    let turn = next.turnsById[activity.turnId];
    if (!turn) {
      const previousTurn = current.turnsById[activity.turnId];
      if (!previousTurn) continue;
      turn = {
        ...previousTurn,
        messageIds: previousTurn.messageIds.filter((messageId) => Boolean(next.messagesById[messageId])),
        activityIds: [],
      };
      next.turnsById[activity.turnId] = turn;
      next.turnOrder.push(activity.turnId);
    }
    if (!turn.activityIds.includes(activityId)) turn.activityIds.push(activityId);
  }
  return next;
}

/** Settle confirmed replay before restoring local prompt admissions. */
function settleAgentSnapshotQuiescence(
  next: AgentProjectionState,
  snapshot: AgentSnapshot,
  replayStatus = next.status,
): void {
  const authoritativeQuiescent = (snapshot.runtimeQuiescent === true || !snapshot.partial)
    && Boolean(snapshot.status)
    && ['idle', 'ready', 'stopped', 'active', 'failed', 'faulted'].includes(snapshot.status ?? '');
  if (authoritativeQuiescent && snapshot.status) {
    next.status = snapshot.status;
    // `status` is the Runtime's authoritative process boundary. A bounded
    // event journal can end after `message_completed` without retaining the
    // matching `turn_completed`, or can retain an old Tool start after its
    // final receipt rolled out of the window. Once the Session is quiescent,
    // no restored turn may keep a spinner alive. Settle every such snapshot
    // turn here, before local optimistic admissions are restored below.
    for (const turnId of next.turnOrder) {
      const turn = next.turnsById[turnId];
      if (!turn) continue;
      const hasPendingHumanApproval = turn.activityIds.some((activityId) => {
        const activity = next.activitiesById[activityId];
        return activity?.kind === 'approval_required'
          && activity.status === 'waiting'
          && approvalNeedsHumanDecision(activity.payload);
      });
      if (hasPendingHumanApproval) continue;
      const hasLiveActivity = turn.activityIds.some((activityId) => {
        const activity = next.activitiesById[activityId];
        return activity?.status === 'running' || activity?.status === 'waiting';
      });
      if (!hasLiveActivity && !['queued', 'running', 'waiting'].includes(turn.status)) continue;
      const hasAbortedTranscript = turn.messageIds.some(
        (messageId) => next.messagesById[messageId]?.status === 'aborted',
      );
      completeTurn(
        next,
        turn.id,
        snapshot.status === 'failed' || snapshot.status === 'faulted'
          ? 'failed'
          : replayStatus === 'aborting' || hasAbortedTranscript ? 'aborted' : 'completed',
        turn.updatedAtMs,
        '',
        true,
      );
    }
  }

}

/** A slow full-history read can finish behind SSE. Import missing durable
 * history without moving the live cursor, status, or existing receipts back. */
export function mergeAgentSnapshotHistory(
  current: AgentProjectionState,
  snapshot: AgentSnapshot,
): AgentProjectionState {
  if (snapshot.partial || snapshot.messages.length === 0) return current;
  const history = applyAgentSnapshot(createAgentProjection(current.sessionId), snapshot);
  history.activityOrder = history.activityOrder.filter((id) => (
    history.activitiesById[id].status !== 'running' && history.activitiesById[id].status !== 'waiting'
  ));
  const next = cloneState(current);
  const transcriptIds = new Set<string>();
  let changed = false;
  for (const id of history.messageOrder) {
    const message = history.messagesById[id];
    if (message.status !== 'completed' || current.messagesById[id]) continue;
    const turnStatus = next.turnsById[message.turnId]?.status;
    upsertMessage(next, message);
    if (turnStatus) next.turnsById[message.turnId].status = turnStatus;
    else next.turnsById[message.turnId].status = history.turnsById[message.turnId].status;
    transcriptIds.add(id);
    changed = true;
  }
  const merged = preserveConfirmedSnapshotActivities(history, next);
  changed ||= merged.activityOrder.length !== current.activityOrder.length;
  if (!changed) return current;
  reconcileTranscriptReplayMessages(merged, transcriptIds, new Set());
  merged.messageOrder.sort((a, b) => merged.messagesById[a].createdAtMs - merged.messagesById[b].createdAtMs);
  merged.turnOrder.sort((a, b) => merged.turnsById[a].createdAtMs - merged.turnsById[b].createdAtMs);
  return merged;
}

export function applyAgentSnapshot(
  state: AgentProjectionState,
  snapshot: AgentSnapshot,
  options: { preserveConfirmedActivities?: boolean } = {},
): AgentProjectionState {
  const inFlightAdmissionClientIds = new Set(
    Object.entries(state.optimisticByClientMessageId).flatMap(([
      clientMessageId,
      messageId,
    ]) => {
      const message = state.messagesById[messageId];
      return message
        && message.role === 'user'
        && (message.status === 'queued' || message.status === 'streaming')
        && !message.admissionState
        ? [clientMessageId]
        : [];
    }),
  );
  let next = createAgentProjection(state.sessionId);
  next.status = snapshot.status ?? state.status;
  next.telemetry = parseTelemetry(snapshot.telemetry) ?? state.telemetry;
  next.messageQueue = parseMessageQueue(snapshot.messageQueue);
  next.todo = parseAgentTodo(snapshot.todo) ?? state.todo;
  next.goal = parseAgentGoal(snapshot.goal) ?? state.goal;
  next.actGate = parseActGate(
    snapshot.actGate,
    next.todo.revision,
    next.goal.revision,
  ) ?? state.actGate;
  for (const value of Array.isArray(snapshot.backgroundJobs) ? snapshot.backgroundJobs : []) {
    const job = parseBackgroundJob(value);
    if (job && job.sessionId === state.sessionId) upsertBackgroundJob(next, job);
  }
  for (const value of Array.isArray(snapshot.lifecycleCancellationAudits) ? snapshot.lifecycleCancellationAudits : []) {
    const audit = parseLifecycleCancellationAudit(value);
    if (audit && audit.sessionId === state.sessionId) upsertLifecycleCancellationAudit(next, audit);
  }

  const serverClientIds = new Set<string>();
  const transcriptMessageIds = new Set<string>();
  /* A bounded snapshot carries forward both prior transcript anchors and
     message_completed rows seen only through SSE. Only ids present in this
     response (plus already-normalized history turns) are transcript anchors;
     otherwise the carried SSE row cannot be reconciled with Pi's later id. */
  const currentTranscriptMessageIds = new Set(
    snapshot.messages.flatMap((rawMessage) => {
      const id = record(rawMessage).id;
      return typeof id === 'string' && id ? [id] : [];
    }),
  );
  const snapshotMessages = snapshot.snapshotScope === 'recent' && snapshot.partial === true
    ? mergeBoundedRecentMessages(state, snapshot.messages)
    : snapshot.messages;
  for (const rawMessage of snapshotMessages) {
    const parsed = tryParseAgentMessage(rawMessage);
    if (!parsed.ok || parsed.value.sessionId !== state.sessionId) {
      appendDiagnostic(next, {
        id: `snapshot-invalid:${next.diagnostics.length}`,
        streamKind: 'agent',
        eventType: 'snapshot_message_invalid',
        summary: 'A malformed snapshot message was skipped.',
        sequence: next.lastSequence,
        payload: {},
      });
      continue;
    }
    upsertMessage(next, parsed.value);
    if (
      currentTranscriptMessageIds.has(parsed.value.id)
      || parsed.value.turnId.startsWith('history:')
    ) transcriptMessageIds.add(parsed.value.id);
    for (const clientMessageId of messageIdentityAliases(parsed.value)) {
      serverClientIds.add(clientMessageId);
    }
  }

  // The transcript restores durable conversation text; the bounded live event
  // projection restores current reasoning, tool and approval state. Snapshot
  // events are normalized locally so their historical sequence gaps do not
  // trigger another snapshot. The server cursor below remains authoritative
  // for the following SSE subscription.
  for (const rawEvent of snapshot.liveEvents) {
    try {
      const parsed = parseAgentEvent(rawEvent);
      if (parsed.sessionId !== state.sessionId) throw new TypeError('foreign snapshot event');
      const hydrated = {
        ...parsed,
        sequence: next.lastSequence + 1,
      };
      next = reduceAgentEvent(next, hydrated).state;
    } catch {
      appendDiagnostic(next, {
        id: `snapshot-event-invalid:${next.diagnostics.length}`,
        streamKind: 'agent',
        eventType: 'snapshot_event_invalid',
        summary: 'A malformed snapshot event was skipped.',
        sequence: snapshot.lastSequence,
        payload: {},
      });
    }
  }

  // A restored snapshot carries two views of the same completed turn:
  // `messages` is Pi's authoritative transcript, while `liveEvents` is the
  // bounded product journal needed to rebuild Tool/approval/telemetry state.
  // Pi transcript ids and product event ids intentionally differ, so replaying
  // message events verbatim used to render one user/assistant pair twice. Keep
  // the Pi message as the public anchor, absorb event-only metadata such as the
  // clientMessageId, and remove only a narrowly matched replay copy. In-flight
  // deltas remain untouched because they have no completed transcript match.
  reconcileTranscriptReplayMessages(next, transcriptMessageIds, serverClientIds);
  if (snapshot.lastSequence === state.lastSequence) {
    reconcileEqualCursorAcceptedMessages(
      state,
      next,
      transcriptMessageIds,
      serverClientIds,
    );
  }
  reconcileSnapshotOptimisticMessages(
    state,
    next,
    transcriptMessageIds,
    serverClientIds,
  );

  // liveEvents is a bounded journal and may end with an old busy/aborting
  // marker after a runtime restart. `active` means the persisted Pi transcript
  // is open; only `busy`/`working`/`waiting` mean a turn is running. Treat an
  // active-but-quiescent snapshot as terminal so reopening an old conversation
  // cannot turn its last completed answer into a multi-day "thinking" turn.
  // Transcript attachment creates a provisional running turn. Resolve that
  // placeholder before applying a failure boundary, so an old completed
  // answer is not rewritten as failed when a later tool loses its terminal.
  reconcileSnapshotTurnStatuses(next, false);
  if (options.preserveConfirmedActivities || snapshot.partial === true) {
    next = preserveConfirmedSnapshotActivities(state, next);
  }
  settleAgentSnapshotQuiescence(next, snapshot, options.preserveConfirmedActivities ? state.status : next.status);

  for (const [clientMessageId, messageId] of Object.entries(
    state.optimisticByClientMessageId,
  )) {
    if (serverClientIds.has(clientMessageId)) continue;
    const optimistic = state.messagesById[messageId];
    if (!optimistic) continue;
    next.messagesById[messageId] = optimistic;
    next.messageOrder.push(messageId);
    next.optimisticByClientMessageId[clientMessageId] = messageId;
    attachMessageToTurn(next, optimistic);
    const previousTurn = state.turnsById[optimistic.turnId];
    const restoredTurn = next.turnsById[optimistic.turnId];
    if (previousTurn && restoredTurn) {
      const restoredAtMs = Math.max(restoredTurn.updatedAtMs, previousTurn.updatedAtMs);
      // Session creation publishes the window before its first prompt has
      // necessarily reached Pi. An idle snapshot can therefore be older than
      // this in-flight local admission. Only the request result or an exact
      // durable clientMessageId may settle it; snapshot status alone cannot.
      next.turnsById[optimistic.turnId] = {
        ...restoredTurn,
        status: previousTurn.status,
        updatedAtMs: restoredAtMs,
        failure: previousTurn.failure,
      };
    }
  }
  const hasUnsettledAdmission = Object.values(next.optimisticByClientMessageId).some((messageId) => {
    const status = next.messagesById[messageId]?.status;
    return status === 'queued' || status === 'streaming';
  }) || next.messageOrder.some((messageId) => {
    const message = next.messagesById[messageId];
    if (
      !message
      || message.role !== 'user'
      || !message.clientMessageId
      || !inFlightAdmissionClientIds.has(message.clientMessageId)
    ) return false;
    const turn = next.turnsById[message.turnId];
    return Boolean(turn) && !turn.messageIds.some(
      (turnMessageId) => next.messagesById[turnMessageId]?.role === 'assistant',
    );
  });
  // A snapshot may observe Pi's durable user anchor before it observes the
  // matching Runtime transition away from idle. That anchor proves admission,
  // not completion. Preserve the in-flight turn until an assistant/terminal
  // event or the request path reports a real failure.
  if (hasUnsettledAdmission) {
    next.status = 'busy';
  }
  reconcileSnapshotTurnStatuses(next);
  next.lastSequence = Math.max(0, snapshot.lastSequence);
  next.lastEventId = snapshot.resumeToken;
  next.resumeToken = snapshot.resumeToken;
  next.needsSnapshot = false;
  next.gap = undefined;
  return next;
}

/**
 * Current Pi transcripts project the durable turn binding and exact
 * clientMessageId. Legacy transcripts can lack that binding, so a refresh may
 * still contain the accepted user message while the local optimistic copy
 * looks unrelated. Restoring both creates a second queued turn after the real
 * turn completes and misroutes the next prompt as Steer.
 *
 * The fallback is legacy-only: match one otherwise-unsettled local user
 * message to one exact, nearby durable user message. Failed, pending, or
 * ambiguous admissions remain local; unbounded text-only matching is forbidden.
 */
function reconcileSnapshotOptimisticMessages(
  previous: AgentProjectionState,
  snapshot: AgentProjectionState,
  transcriptMessageIds: ReadonlySet<string>,
  serverClientIds: Set<string>,
): void {
  const claimedTranscriptIds = new Set<string>();
  for (const [clientMessageId, optimisticId] of Object.entries(
    previous.optimisticByClientMessageId,
  )) {
    if (serverClientIds.has(clientMessageId)) continue;
    const optimistic = previous.messagesById[optimisticId];
    if (
      !optimistic
      || optimistic.role !== 'user'
      || optimistic.status !== 'queued'
      || optimistic.admissionState
    ) continue;
    const fingerprint = replayFingerprint(optimistic);
    if (!fingerprint) continue;
    const candidates = [...transcriptMessageIds]
      .filter((messageId) => !claimedTranscriptIds.has(messageId))
      .map((messageId) => snapshot.messagesById[messageId])
      .filter((message): message is AgentMessageProjection => (
        Boolean(message)
        && message.role === 'user'
        && replayFingerprint(message) === fingerprint
        // A transcript row older than the local admission can only belong to
        // an earlier repeated prompt. The accepted current row is created by
        // the local Runtime after the optimistic admission timestamp.
        && message.createdAtMs >= optimistic.createdAtMs
        && Math.abs(message.createdAtMs - optimistic.createdAtMs) <= 60_000
      ))
      .sort((left, right) => (
        Math.abs(left.createdAtMs - optimistic.createdAtMs)
        - Math.abs(right.createdAtMs - optimistic.createdAtMs)
      ));
    // A repeated user prompt can produce several indistinguishable legacy
    // transcript rows inside this narrow time window. Guessing would settle
    // the newest optimistic admission against an older turn. Only a unique
    // legacy candidate is safe; current transcripts use clientMessageId.
    if (candidates.length !== 1) continue;
    const candidate = candidates[0];
    claimedTranscriptIds.add(candidate.id);
    serverClientIds.add(clientMessageId);
    snapshot.messagesById[candidate.id] = {
      ...inheritLocalDeliveryProjection(candidate, optimistic),
      clientMessageId,
      ...(optimistic.retryOfClientMessageId
        ? { retryOfClientMessageId: optimistic.retryOfClientMessageId }
        : {}),
    };
  }
}

export function applyAgentBackgroundJobReceipt(
  state: AgentProjectionState,
  value: unknown,
): AgentProjectionState {
  const job = parseBackgroundJob(record(value).job);
  if (!job || job.sessionId !== state.sessionId) return state;
  const current = state.backgroundJobsById[job.jobId];
  if (
    current
    && (
      current.updatedAtMs > job.updatedAtMs
      || (
        current.updatedAtMs === job.updatedAtMs
        && BACKGROUND_JOB_STATUS_PRECEDENCE[current.status]
          > BACKGROUND_JOB_STATUS_PRECEDENCE[job.status]
      )
    )
  ) return state;
  const next = cloneState(state);
  upsertBackgroundJob(next, job);
  return next;
}

function reconcileTranscriptReplayMessages(
  state: AgentProjectionState,
  transcriptMessageIds: ReadonlySet<string>,
  serverClientIds: Set<string>,
): void {
  const transcriptByFingerprint = new Map<string, AgentMessageProjection[]>();
  const transcriptByMediaShapeFingerprint = new Map<string, AgentMessageProjection[]>();
  for (const messageId of transcriptMessageIds) {
    const message = state.messagesById[messageId];
    const fingerprint = message ? replayFingerprint(message) : '';
    if (!message || !fingerprint) continue;
    transcriptByFingerprint.set(
      fingerprint,
      [...(transcriptByFingerprint.get(fingerprint) ?? []), message],
    );
    const mediaShapeFingerprint = replayMediaShapeFingerprint(message);
    transcriptByMediaShapeFingerprint.set(
      mediaShapeFingerprint,
      [...(transcriptByMediaShapeFingerprint.get(mediaShapeFingerprint) ?? []), message],
    );
  }

  const claimedTranscriptIds = new Set<string>();
  const replayTurnAnchors = new Map<string, string>();
  for (const messageId of [...state.messageOrder]) {
    if (transcriptMessageIds.has(messageId)) continue;
    const replay = state.messagesById[messageId];
    if (!replay || !['completed', 'streaming'].includes(replay.status) || replay.timelineSequence === undefined) continue;
    const fingerprint = replayFingerprint(replay);
    if (!fingerprint) continue;
    const nearbyCandidates = (
      messages: AgentMessageProjection[],
      maxDistanceMs = 5_000,
    ) => messages
      .filter((message) => !claimedTranscriptIds.has(message.id))
      .map((message) => ({
        message,
        distance: Math.abs(message.createdAtMs - replay.createdAtMs),
      }))
      .filter(({ distance }) => distance <= maxDistanceMs)
      .sort((left, right) => left.distance - right.distance);
    const exactCandidate = nearbyCandidates(
      transcriptByFingerprint.get(fingerprint) ?? [],
    )[0]?.message;
    // Pi's live final message retains the Provider request start, while its
    // durable transcript is timestamped when appended. A long stream can
    // exceed the short replay window. The Runtime's final-message alias and
    // one matching completed transcript within that exact turn are stronger
    // identity evidence; do not extend time-based matching for other turns.
    const finalAlias = replay.role === 'assistant' && (
      replay.id === `${replay.turnId}:assistant`
      || replay.id.startsWith(`${replay.turnId}:assistant:segment:`)
    );
    const sameTurnFinals = finalAlias ? (transcriptByFingerprint.get(fingerprint) ?? [])
      .filter((message) => message.turnId === replay.turnId && message.status === 'completed' && !claimedTranscriptIds.has(message.id)) : [];
    const sameTurnFinal = sameTurnFinals.length === 1 ? sameTurnFinals[0] : undefined;
    /* The accepted upload and Pi's persisted inline image can be imported as
       two managed-media receipts with different ids. The event-proven
       clientMessageId, exact visible text, equal attachment count, one nearby
       transcript candidate, and a tighter one-second media window together
       identify one send without broadly folding later same-text messages. */
    const mediaShapeCandidates = (
      replay.role === 'user'
      && Boolean(replay.clientMessageId)
      && replay.attachments.length > 0
    )
      ? nearbyCandidates(
          transcriptByMediaShapeFingerprint.get(replayMediaShapeFingerprint(replay)) ?? [],
          1_000,
        )
      : [];
    const mediaShapeCandidate = mediaShapeCandidates.length === 1
      ? mediaShapeCandidates[0]?.message
      : undefined;
    // A missing message_end leaves a live alias streaming after Pi has persisted it.
    // Only a unique exact same-turn durable row can settle that alias.
    const candidate = replay.status === 'streaming'
      ? sameTurnFinal
      : exactCandidate ?? sameTurnFinal ?? mediaShapeCandidate;
    if (!candidate) continue;

    claimedTranscriptIds.add(candidate.id);
    if (replay.turnId !== candidate.turnId) {
      const existingAnchor = replayTurnAnchors.get(replay.turnId);
      if (!existingAnchor || existingAnchor === candidate.turnId) {
        replayTurnAnchors.set(replay.turnId, candidate.turnId);
      }
    }
    const clientMessageId = candidate.clientMessageId || replay.clientMessageId;
    state.messagesById[candidate.id] = {
      ...candidate,
      ...(clientMessageId ? { clientMessageId } : {}),
      ...(candidate.provider || !replay.provider ? {} : { provider: replay.provider }),
      ...(candidate.model || !replay.model ? {} : { model: replay.model }),
      ...(candidate.usage || !replay.usage ? {} : { usage: replay.usage }),
    };
    if (clientMessageId) serverClientIds.add(clientMessageId);
    removeProjectedMessage(state, replay);
  }
  reconcileReplayTurnAnchors(state, replayTurnAnchors);
}

/**
 * A quiet post-admission snapshot can arrive after the durable user SSE but at
 * the same cursor. Pi's transcript then names the active turn `history:*`,
 * while the already-applied event names the same turn by its Runtime turnId.
 * Preserve the event-proven identity so the later terminal event cannot leave
 * the synthetic alias running beside the completed real turn.
 */
function reconcileEqualCursorAcceptedMessages(
  previous: AgentProjectionState,
  snapshot: AgentProjectionState,
  transcriptMessageIds: ReadonlySet<string>,
  serverClientIds: Set<string>,
): void {
  const claimedTranscriptIds = new Set<string>();
  const transcriptTurnAliases = new Map<string, string>();
  for (const messageId of previous.messageOrder) {
    const accepted = previous.messagesById[messageId];
    const clientMessageId = accepted?.clientMessageId ?? '';
    const acceptedTurnStatus = accepted
      ? previous.turnsById[accepted.turnId]?.status
      : undefined;
    if (
      !accepted
      || accepted.role !== 'user'
      || accepted.status !== 'completed'
      || accepted.timelineSequence === undefined
      || !clientMessageId
      || !acceptedTurnStatus
      || !['queued', 'running', 'waiting'].includes(acceptedTurnStatus)
    ) continue;
    const fingerprint = replayFingerprint(accepted);
    if (!fingerprint) continue;
    const candidate = [...transcriptMessageIds]
      .filter((candidateId) => !claimedTranscriptIds.has(candidateId))
      .map((candidateId) => snapshot.messagesById[candidateId])
      .filter((message): message is AgentMessageProjection => (
        Boolean(message)
        && message.role === 'user'
        && (!message.clientMessageId || message.clientMessageId === clientMessageId)
        && replayFingerprint(message) === fingerprint
        && Math.abs(message.createdAtMs - accepted.createdAtMs) <= 5_000
      ))
      .sort((left, right) => (
        Math.abs(left.createdAtMs - accepted.createdAtMs)
        - Math.abs(right.createdAtMs - accepted.createdAtMs)
      ))[0];
    if (!candidate) continue;

    claimedTranscriptIds.add(candidate.id);
    serverClientIds.add(clientMessageId);
    snapshot.messagesById[candidate.id] = {
      ...inheritLocalDeliveryProjection(candidate, accepted),
      clientMessageId,
      ...(accepted.retryOfClientMessageId
        ? { retryOfClientMessageId: accepted.retryOfClientMessageId }
        : {}),
    };
    if (candidate.turnId !== accepted.turnId) {
      const existingAlias = transcriptTurnAliases.get(candidate.turnId);
      if (!existingAlias || existingAlias === accepted.turnId) {
        transcriptTurnAliases.set(candidate.turnId, accepted.turnId);
      }
    }
  }
  reconcileReplayTurnAnchors(snapshot, transcriptTurnAliases);
}

/**
 * A restored snapshot has two identifiers for one logical turn: Pi's durable
 * transcript uses `history:<user-message-id>`, while the bounded Runtime
 * journal retains the original request turnId. Matching a replayed completed
 * message to its transcript anchor proves those identifiers are aliases.
 * Move the remaining Tool/reasoning metadata to that anchor so a settled turn
 * cannot render as a second, message-less "waiting for reply" turn.
 */
function reconcileReplayTurnAnchors(
  state: AgentProjectionState,
  replayTurnAnchors: ReadonlyMap<string, string>,
): void {
  for (const [replayTurnId, transcriptTurnId] of replayTurnAnchors) {
    if (replayTurnId === transcriptTurnId) continue;
    const replayTurn = writableTurn(state, replayTurnId);
    if (!replayTurn) continue;
    const transcriptTurn = ensureTurn(
      state,
      transcriptTurnId,
      replayTurn.createdAtMs,
    );

    for (const messageId of replayTurn.messageIds) {
      const message = state.messagesById[messageId];
      if (!message) continue;
      state.messagesById[messageId] = {
        ...message,
        turnId: transcriptTurnId,
      };
      if (!transcriptTurn.messageIds.includes(messageId)) {
        transcriptTurn.messageIds.push(messageId);
      }
    }
    for (const activityId of replayTurn.activityIds) {
      const activity = state.activitiesById[activityId];
      if (!activity) continue;
      state.activitiesById[activityId] = {
        ...activity,
        turnId: transcriptTurnId,
      };
      if (!transcriptTurn.activityIds.includes(activityId)) {
        transcriptTurn.activityIds.push(activityId);
      }
    }
    transcriptTurn.createdAtMs = Math.min(
      transcriptTurn.createdAtMs,
      replayTurn.createdAtMs,
    );
    transcriptTurn.updatedAtMs = Math.max(
      transcriptTurn.updatedAtMs,
      replayTurn.updatedAtMs,
    );
    if (replayTurn.failure && !transcriptTurn.failure) {
      transcriptTurn.failure = replayTurn.failure;
    }
    delete state.turnsById[replayTurnId];
    state.turnOrder = state.turnOrder.filter((turnId) => turnId !== replayTurnId);
  }
}

function replayFingerprint(message: AgentMessageProjection): string {
  const visibleText = replayVisibleText(message);
  if (!visibleText) return '';
  return JSON.stringify([
    message.role,
    visibleText,
    [...message.attachments],
  ]);
}

function replayMediaShapeFingerprint(message: AgentMessageProjection): string {
  const visibleText = replayVisibleText(message);
  if (!visibleText) return '';
  return JSON.stringify([
    message.role,
    visibleText,
    message.attachments.length,
  ]);
}

function replayVisibleText(message: AgentMessageProjection): string {
  return message.blocks
    // Frozen file previews may be enriched between SSE and transcript reads.
    // Their source text is an attachment, not part of the spoken answer.
    .filter((block) => message.role !== 'assistant' || block.type === 'text')
    .map((block) => text(record(block.data).text))
    .filter(Boolean)
    .join('\n')
    .replace(/\s+/gu, ' ')
    .trim();
}

function removeProjectedMessage(
  state: AgentProjectionState,
  message: AgentMessageProjection,
): void {
  delete state.messagesById[message.id];
  state.messageOrder = state.messageOrder.filter((messageId) => messageId !== message.id);
  detachMessageFromTurn(state, message);
  const turn = state.turnsById[message.turnId];
  if (!turn || turn.messageIds.length > 0 || turn.activityIds.length > 0) return;
  delete state.turnsById[message.turnId];
  state.turnOrder = state.turnOrder.filter((turnId) => turnId !== message.turnId);
}

export function agentSnapshotFromResponse(value: unknown): AgentSnapshot {
  const payload = record(value);
  const messages = Array.isArray(payload.messages)
    ? payload.messages
    : Array.isArray(payload.items)
      ? payload.items
      : [];
  return {
    messages,
    liveEvents: Array.isArray(payload.liveEvents) ? payload.liveEvents : [],
    lastSequence: integer(payload.lastSequence ?? payload.lastEventSequence),
    resumeToken: text(payload.resumeToken ?? payload.lastEventId),
    ...(payload.snapshotScope === 'recent' ? { snapshotScope: 'recent' as const } : {}),
    ...(payload.partial === true ? { partial: true } : {}),
    ...(payload.runtimeQuiescent === true ? { runtimeQuiescent: true } : {}),
    ...(typeof payload.status === 'string' ? { status: payload.status } : {}),
    ...(payload.telemetry === undefined ? {} : { telemetry: payload.telemetry }),
    ...(payload.messageQueue === undefined ? {} : { messageQueue: payload.messageQueue }),
    ...(payload.todo === undefined ? {} : { todo: payload.todo }),
    ...(payload.goal === undefined ? {} : { goal: payload.goal }),
    ...(payload.actGate === undefined ? {} : { actGate: payload.actGate }),
    ...(payload.backgroundJobs === undefined ? {} : { backgroundJobs: payload.backgroundJobs }),
    ...(payload.lifecycleCancellationAudits === undefined ? {} : { lifecycleCancellationAudits: payload.lifecycleCancellationAudits }),
  };
}

function applyTextDelta(
  state: AgentProjectionState,
  event: UiAgentEvent,
  payload: Record<string, unknown>,
): void {
  const delta = text(payload.delta);
  if (!delta) return;
  const baseMessageId = text(payload.messageId) || `${event.turnId}:assistant`;
  const replaceContent = payload.replaceContent === true;
  const messageId = streamingAssistantSegmentId(
    state,
    event.turnId,
    baseMessageId,
    payload.replaceBlock === true && !replaceContent,
    event.sequence,
  );
  const blockId = messageId === baseMessageId
    ? text(payload.blockId) || `${messageId}:text`
    : `${messageId}:text`;
  const existing = state.messagesById[messageId];
  const blocks = existing ? [...existing.blocks] : [];
  const blockIndex = blocks.findIndex((block) => block.id === blockId || block.type === 'text');
  if (blockIndex >= 0) {
    const block = blocks[blockIndex];
    const previous = replaceContent ? '' : text(record(block.data).text);
    blocks[blockIndex] = {
      ...block,
      status: 'running',
      type: 'text',
      presentationKind: 'markdown',
      data: { ...record(block.data), text: previous + delta },
    };
  } else {
    blocks.push({
      id: blockId,
      type: 'text',
      status: 'running',
      presentationKind: 'markdown',
      data: { text: delta },
    });
  }
  const message: AgentMessageProjection = existing
    ? { ...existing, status: 'streaming', blocks, completedAtMs: null }
    : {
        schemaVersion: 'rag-ime.agent-message.v1',
        id: messageId,
        sessionId: event.sessionId,
        turnId: event.turnId,
        role: 'assistant',
        status: 'streaming',
        blocks,
        attachments: [],
        citations: [],
        createdAtMs: event.createdAtMs,
        completedAtMs: null,
        timelineSequence: sourceTimelineSequence(event),
      };
  upsertMessage(state, message);
  reopenProvisionalTurn(state, event.turnId, event.createdAtMs);
  state.status = 'responding';
}

function applyCompletedMessage(
  state: AgentProjectionState,
  event: UiAgentEvent,
  payload: Record<string, unknown>,
): void {
  const parsed = tryParseAgentMessage(payload.message);
  if (!parsed.ok) {
    appendDiagnostic(state, {
      id: `${event.eventId}:invalid-message`,
      streamKind: 'agent',
      eventType: 'message_completed_invalid',
      summary: 'A malformed completed message was skipped.',
      sequence: event.sequence,
      payload,
    });
    return;
  }
  const clientMessageId = text(payload.clientMessageId) || parsed.value.clientMessageId || '';
  const usage = parseUsage(payload.usage);
  const model = state.telemetry?.model;
  const enriched = parsed.value.role === 'assistant'
    ? {
        ...parsed.value,
        ...(parsed.value.usage || !usage ? {} : { usage }),
        ...(parsed.value.provider || !model?.provider ? {} : { provider: model.provider }),
        ...(parsed.value.model || !model?.id ? {} : { model: model.id }),
      }
    : parsed.value;
  const completedMessage = enriched.role === 'assistant'
    ? completedAssistantSegment(state, event, enriched)
    : {
        ...parsed.value,
        timelineSequence: parsed.value.timelineSequence ?? sourceTimelineSequence(event),
      };
  // A Session is navigated into before Pi has appended its first user row.
  // That row may arrive as a durable `message_completed` SSE event without the
  // product clientMessageId, leaving a local optimistic bubble beside the real
  // row. Reconcile one bounded local admission by its exact message fingerprint
  // and nearby Runtime timestamp; failed, pending, ambiguous, and unrelated
  // external admissions stay auditable and untouched.
  const inferredClientMessageId = clientMessageId || (
    completedMessage.role === 'user'
      ? matchingLocalOptimisticClientMessageId(state, completedMessage)
        || matchingRoomMirrorClientMessageId(state, completedMessage)
      : ''
  );
  upsertMessage(
    state,
    inferredClientMessageId
      ? { ...completedMessage, clientMessageId: inferredClientMessageId }
      : completedMessage,
    inferredClientMessageId,
  );
  if (parsed.value.status === 'failed' || parsed.value.status === 'aborted') {
    touchTurn(
      state,
      parsed.value.turnId,
      parsed.value.status === 'failed' ? 'failed' : 'aborted',
      event.createdAtMs,
    );
  } else {
    reopenProvisionalTurn(state, parsed.value.turnId, event.createdAtMs);
  }
}

function matchingLocalOptimisticClientMessageId(
  state: AgentProjectionState,
  durableMessage: AgentMessageProjection,
): string {
  const durableAliases = new Set(messageIdentityAliases(durableMessage));
  const exactAlias = Object.entries(state.optimisticByClientMessageId)
    .find(([clientMessageId, messageId]) => {
      const optimistic = state.messagesById[messageId];
      return Boolean(
        optimistic
        && isLocalAdmissionClientMessageId(clientMessageId)
        && optimistic.role === 'user'
        && optimistic.status === 'queued'
        && !optimistic.admissionState
        && durableAliases.has(clientMessageId)
      );
    });
  if (exactAlias) return exactAlias[0];
  const fingerprint = replayFingerprint(durableMessage);
  if (!fingerprint) return '';
  const candidates = Object.entries(state.optimisticByClientMessageId)
    .flatMap(([clientMessageId, messageId]) => {
      const optimistic = state.messagesById[messageId];
      if (
        !optimistic
        || !isLocalAdmissionClientMessageId(clientMessageId)
        || optimistic.role !== 'user'
        || optimistic.status !== 'queued'
        || optimistic.admissionState
        || replayFingerprint(optimistic) !== fingerprint
        || Math.abs(optimistic.createdAtMs - durableMessage.createdAtMs) > 5_000
      ) return [];
      return [{ clientMessageId, distance: Math.abs(optimistic.createdAtMs - durableMessage.createdAtMs) }];
    })
    .sort((left, right) => left.distance - right.distance);
  // Two equal nearby prompts are not enough evidence to assign identity. The
  // next snapshot has the broader transcript reconciliation with the same
  // conservative rule, so do not silently merge a legitimate duplicate here.
  if (!candidates[0] || candidates[0].distance === candidates[1]?.distance) return '';
  return candidates[0].clientMessageId;
}

function isLocalAdmissionClientMessageId(clientMessageId: string): boolean {
  // These prefixes are minted by the Home, PAWOS, and web Agent composers.
  // Keep the inference opt-in: arbitrary client ids must not be merged by text
  // and time alone, and Room mirrors have their own source-bound matcher.
  return (
    clientMessageId.startsWith('session-')
    || clientMessageId.startsWith('paw-')
    || clientMessageId.startsWith('web-')
  );
}

function matchingRoomMirrorClientMessageId(
  state: AgentProjectionState,
  durableMessage: AgentMessageProjection,
): string {
  const fingerprint = replayFingerprint(durableMessage);
  if (!fingerprint) return '';
  const candidates = state.messageOrder
    .map((messageId) => state.messagesById[messageId])
    .filter((message): message is AgentMessageProjection => (
      Boolean(message)
      && message.role === 'user'
      && messageIdentityAliases(message).length > 0
      && message.blocks.some((block) => block.source?.kind === 'room_event')
      && replayFingerprint(message) === fingerprint
      && Math.abs(message.createdAtMs - durableMessage.createdAtMs) <= 60_000
    ))
    .sort((left, right) => (
      Math.abs(left.createdAtMs - durableMessage.createdAtMs)
      - Math.abs(right.createdAtMs - durableMessage.createdAtMs)
    ));
  if (!candidates[0]) return '';
  const firstDistance = Math.abs(candidates[0].createdAtMs - durableMessage.createdAtMs);
  const secondDistance = candidates[1]
    ? Math.abs(candidates[1].createdAtMs - durableMessage.createdAtMs)
    : -1;
  return firstDistance === secondDistance
    ? ''
    : messageIdentityAliases(candidates[0])[0] ?? '';
}

/**
 * A Room mirror carries the original UI id in text-block data while the Pi
 * transcript uses the bound dispatch id as its canonical clientMessageId.
 * Keep both identities available to snapshot, SSE, and optimistic admission
 * reconciliation. The aliases are source-bound; text and time alone remain
 * the legacy fallback below.
 */
function messageIdentityAliases(message: AgentMessageProjection): string[] {
  const aliases = new Set<string>();
  if (message.clientMessageId) aliases.add(message.clientMessageId);
  for (const block of message.blocks) {
    if (block.type !== 'text') continue;
    const data = record(block.data);
    for (const key of ['roomClientMessageId', 'roomDispatchId']) {
      const value = text(data[key]);
      if (value) aliases.add(value);
    }
  }
  return [...aliases];
}

function isRoomMirrorMessage(message: AgentMessageProjection): boolean {
  return message.blocks.some((block) => block.source?.kind === 'room_event');
}

function retainRoomIdentityAliases(
  existing: AgentMessageProjection,
  mirror: AgentMessageProjection,
): AgentMessageProjection {
  const mirrorData = mirror.blocks
    .filter((block) => block.type === 'text')
    .map((block) => record(block.data))
    .find((data) => (
      text(data.roomClientMessageId)
      || text(data.roomDispatchId)
      || text(data.roomEventId)
    ));
  if (!mirrorData) return existing;
  let changed = false;
  const blocks = existing.blocks.map((block) => {
    if (changed || block.type !== 'text') return block;
    const data = { ...block.data };
    for (const key of ['roomClientMessageId', 'roomDispatchId', 'roomEventId']) {
      const value = text(mirrorData[key]);
      if (value && !text(data[key])) {
        data[key] = value;
        changed = true;
      }
    }
    return changed ? { ...block, data } : block;
  });
  return changed ? { ...existing, blocks } : existing;
}

function upsertCompactionActivity(
  state: AgentProjectionState,
  event: UiAgentEvent,
  payload: Record<string, unknown>,
  status: AgentActivityProjection['status'],
): void {
  const runningId = [...state.activityOrder]
    .reverse()
    .find((activityId) => {
      const activity = state.activitiesById[activityId];
      return activity?.kind === 'context_compaction' && activity.status === 'running';
    });
  const id = runningId ?? `compaction:${event.eventId}`;
  const previous = state.activitiesById[id];
  const reason = text(payload.reason);
  const activity: AgentActivityProjection = {
    id,
    turnId: previous?.turnId || event.turnId || `maintenance:${event.sequence}`,
    kind: 'context_compaction',
    status,
    summary: status === 'running' ? '正在压缩上下文' : '上下文压缩完成',
    payload: { ...previous?.payload, ...payload },
    createdAtMs: previous?.createdAtMs ?? event.createdAtMs,
    updatedAtMs: event.createdAtMs,
    timelineSequence: previous?.timelineSequence ?? sourceTimelineSequence(event),
  };
  activity.summary = status === 'failed'
    ? '上下文压缩失败'
    : status === 'running'
      ? `${compactionReasonLabel(reason)}，正在压缩上下文`
      : `${compactionReasonLabel(reason)}，上下文压缩完成`;
  if (!previous) state.activityOrder.push(id);
  state.activitiesById[id] = activity;
  const turn = ensureTurn(state, activity.turnId, activity.createdAtMs);
  if (!turn.activityIds.includes(id)) turn.activityIds.push(id);
}

function compactionReasonLabel(reason: string): string {
  if (reason === 'manual') return '手动触发';
  if (reason === 'overflow') return '溢出恢复';
  if (reason === 'threshold' || reason === 'automatic') return '达到上下文阈值';
  return 'Runtime 触发';
}

function parseTelemetry(value: unknown): AgentSessionTelemetryV1 | undefined {
  if (!value) return undefined;
  const parsed = validateContract('agent-session-telemetry.v1', value);
  return parsed.ok ? parsed.value : undefined;
}

function parseUsage(value: unknown): UiAgentMessage['usage'] | undefined {
  const usage = record(value);
  const fields = ['input', 'output', 'cacheRead', 'cacheWrite', 'totalTokens'] as const;
  if (!fields.every((field) => typeof usage[field] === 'number' && Number.isFinite(usage[field]))) {
    return undefined;
  }
  return {
    input: integer(usage.input),
    output: integer(usage.output),
    cacheRead: integer(usage.cacheRead),
    cacheWrite: integer(usage.cacheWrite),
    totalTokens: integer(usage.totalTokens),
  };
}

function streamingAssistantSegmentId(
  state: AgentProjectionState,
  turnId: string,
  baseMessageId: string,
  startsNewMessage: boolean,
  sequence: number,
): string {
  const segments = assistantSegmentsForBase(state, turnId, baseMessageId);
  const latest = segments[segments.length - 1];
  if (!latest) return baseMessageId;
  if (startsNewMessage) return `${baseMessageId}:segment:${sequence}`;
  return latest.id;
}

function completedAssistantSegment(
  state: AgentProjectionState,
  event: UiAgentEvent,
  message: UiAgentMessage,
): AgentMessageProjection {
  const segments = assistantSegmentsForBase(state, message.turnId, message.id);
  const latest = segments[segments.length - 1];
  const targetId = latest?.status === 'streaming'
    ? latest.id
    : latest
      ? `${message.id}:segment:${event.sequence}`
      : message.id;
  return {
    ...message,
    id: targetId,
    createdAtMs: latest?.status === 'streaming' ? latest.createdAtMs : message.createdAtMs,
    timelineSequence: latest?.status === 'streaming'
      ? latest.timelineSequence ?? message.timelineSequence ?? sourceTimelineSequence(event)
      : message.timelineSequence ?? sourceTimelineSequence(event),
  };
}

function assistantSegmentsForBase(
  state: AgentProjectionState,
  turnId: string,
  baseMessageId: string,
): AgentMessageProjection[] {
  const turn = state.turnsById[turnId];
  if (!turn) return [];
  const segmentPrefix = `${baseMessageId}:segment:`;
  return turn.messageIds.flatMap((messageId) => {
    const message = state.messagesById[messageId];
    if (!message || message.role !== 'assistant') return [];
    return message.id === baseMessageId || message.id.startsWith(segmentPrefix) ? [message] : [];
  });
}

function upsertMessage(
  state: AgentProjectionState,
  message: AgentMessageProjection,
  clientMessageId = message.clientMessageId ?? '',
): void {
  if (message.role !== 'user' && message.role !== 'assistant') return;
  const previous = state.messagesById[message.id];
  if (previous && previous.turnId !== message.turnId) detachMessageFromTurn(state, previous);
  const identityAliases = new Set(messageIdentityAliases(message));
  if (clientMessageId) identityAliases.add(clientMessageId);
  const optimisticId = [...identityAliases]
    .map((identity) => state.optimisticByClientMessageId[identity])
    .find((messageId) => (
      Boolean(messageId)
      && state.messagesById[messageId]?.role === message.role
    ));
  const correlatedId = state.messageOrder.find((messageId) => {
    if (messageId === message.id) return false;
    const existing = state.messagesById[messageId];
    return Boolean(existing)
      && existing.role === message.role
      && messageIdentityAliases(existing)
      .some((identity) => identityAliases.has(identity));
  });
  const correlated = correlatedId ? state.messagesById[correlatedId] : undefined;
  if (
    !optimisticId
    && correlated
    && isRoomMirrorMessage(message)
    && !isRoomMirrorMessage(correlated)
  ) {
    state.messagesById[correlated.id] = retainRoomIdentityAliases(correlated, message);
    return;
  }
  const replaceableId = optimisticId ?? correlatedId;
  let replacedOptimistic = false;
  let projectedMessage = message;
  if (replaceableId && replaceableId !== message.id) {
    const index = state.messageOrder.indexOf(replaceableId);
    const replaced = state.messagesById[replaceableId];
    if (replaced && message.role === 'user') {
      projectedMessage = inheritLocalDeliveryProjection(message, replaced);
    }
    delete state.messagesById[replaceableId];
    for (const [identity, mappedMessageId] of Object.entries(
      state.optimisticByClientMessageId,
    )) {
      if (mappedMessageId === replaceableId) {
        delete state.optimisticByClientMessageId[identity];
      }
    }
    if (index >= 0) state.messageOrder[index] = message.id;
    if (replaced) detachMessageFromTurn(state, replaced);
    replacedOptimistic = index >= 0;
  }

  if (!state.messagesById[projectedMessage.id] && !replacedOptimistic) state.messageOrder.push(projectedMessage.id);
  state.messagesById[projectedMessage.id] = projectedMessage;
  attachMessageToTurn(state, projectedMessage);
}

function inheritLocalDeliveryProjection(
  message: AgentMessageProjection,
  optimistic: AgentMessageProjection,
): AgentMessageProjection {
  const delivery = messageDelivery(optimistic);
  if (!delivery) return message;
  let inherited = false;
  const blocks = message.blocks.map((block) => {
    if (inherited || block.type !== 'text') return block;
    if (text(block.data.delivery)) {
      inherited = true;
      return block;
    }
    inherited = true;
    return { ...block, data: { ...block.data, delivery } };
  });
  return {
    ...message,
    blocks,
    ...(optimistic.deliveryState ? { deliveryState: optimistic.deliveryState } : {}),
    ...(optimistic.deliveryAcceptedAtMs === undefined
      ? {}
      : { deliveryAcceptedAtMs: optimistic.deliveryAcceptedAtMs }),
  };
}

function reconcileMessageDeliveryQueue(
  state: AgentProjectionState,
  previous: AgentMessageQueue,
  current: AgentMessageQueue,
): void {
  const previousByDelivery = {
    steer: new Set(previous.steering.map(normalizedQueueText)),
    followUp: new Set(previous.followUp.map(normalizedQueueText)),
  };
  const currentByDelivery = {
    steer: new Set(current.steering.map(normalizedQueueText)),
    followUp: new Set(current.followUp.map(normalizedQueueText)),
  };
  for (const messageId of state.messageOrder) {
    const message = state.messagesById[messageId];
    const delivery = message ? messageDelivery(message) : '';
    if (!message || (delivery !== 'steer' && delivery !== 'followUp')) continue;
    const messageText = normalizedQueueText(message.blocks
      .map((block) => text(block.data.text))
      .filter(Boolean)
      .join('\n'));
    if (!messageText) continue;
    const isQueued = currentByDelivery[delivery].has(messageText);
    const wasQueued = previousByDelivery[delivery].has(messageText);
    if (isQueued) {
      if (message.deliveryState !== 'applied') {
        state.messagesById[messageId] = { ...message, deliveryState: 'accepted' };
      }
    } else if (wasQueued || message.deliveryState === 'accepted') {
      state.messagesById[messageId] = { ...message, deliveryState: 'applied' };
    }
  }
}

function messageDelivery(message: AgentMessageProjection): 'steer' | 'followUp' | '' {
  const delivery = message.blocks
    .filter((block) => block.type === 'text')
    .map((block) => text(block.data.delivery))
    .find((value) => value === 'steer' || value === 'followUp');
  return delivery === 'steer' || delivery === 'followUp' ? delivery : '';
}

/** Recover the exact delivery identity persisted in the visible user message.
 * Prompt is intentionally represented by the absence of a delivery field in
 * the wire contract, while Steer/Follow-up are stored on the text block. */
export function agentMessageDelivery(
  message: AgentMessageProjection,
): 'prompt' | 'steer' | 'followUp' {
  return messageDelivery(message) || 'prompt';
}
/**
 * Resolve the user input that a failed turn should replay.
 *
 * The Runtime normally links the user row through `turn.messageIds`, but a
 * provider failure can publish the assistant error before the user mirror is
 * attached to that turn. Keep recovery anchored to the current projection:
 * prefer an explicitly linked user, then a same-turn/client-id mirror, and
 * finally a directly adjacent user row in canonical message order.
 */
export function resolveAgentTurnUserMessage(
  projection: AgentProjectionState | undefined,
  turnId: string,
): AgentMessageProjection | undefined {
  if (!projection || !turnId) return undefined;
  const turn = projection.turnsById[turnId];
  if (!turn) return undefined;
  const messages = projection.messageOrder
    .map((messageId) => projection.messagesById[messageId])
    .filter((message): message is AgentMessageProjection => Boolean(message));
  const users = messages.filter((message) => message.role === 'user');
  const turnMessageIds = new Set(turn.messageIds);
  const turnMessages = turn.messageIds
    .map((messageId) => projection.messagesById[messageId])
    .filter((message): message is AgentMessageProjection => Boolean(message));
  const meaningful = (message: AgentMessageProjection): boolean => (
    Boolean(replayVisibleText(message).trim())
    || message.attachments.length > 0
  );
  const rank = (message: AgentMessageProjection): number => (
    (
      message.admissionState === 'ambiguous'
        ? 6
        : message.status === 'failed'
          ? 4
          : message.admissionState
            ? 2
            : 0
    ) + (meaningful(message) ? 1 : 0)
  );
  const choose = (
    candidates: AgentMessageProjection[],
  ): AgentMessageProjection | undefined => {
    let preferredMessage: AgentMessageProjection | undefined;
    let preferredRank = -1;
    for (const message of candidates) {
      const messageRank = rank(message);
      // Equal-rank messages belong to the same turn; the later delivery is
      // the one the user most recently asked to retry.
      if (messageRank >= preferredRank) {
        preferredMessage = message;
        preferredRank = messageRank;
      }
    }
    return preferredMessage && meaningful(preferredMessage)
      ? preferredMessage
      : undefined;
  };

  const linkedUsers = turnMessages.filter((message) => message.role === 'user');
  const linked = choose(linkedUsers);
  if (linked) return linked;

  const sameTurn = users.filter((message) => (
    message.turnId === turnId && !turnMessageIds.has(message.id)
  ));
  const sameTurnUser = choose(sameTurn);
  if (sameTurnUser) return sameTurnUser;

  const relatedClientMessageIds = new Set(
    turnMessages.flatMap((message) => [
      message.clientMessageId,
      message.retryOfClientMessageId,
    ]).filter((value): value is string => Boolean(value)),
  );
  if (relatedClientMessageIds.size > 0) {
    const mirrored = users.filter((message) => (
      typeof message.clientMessageId === 'string'
      && relatedClientMessageIds.has(message.clientMessageId)
    ));
    const mirroredUser = choose(mirrored);
    if (mirroredUser) return mirroredUser;
  }

  const anchorIndexes = turn.messageIds
    .map((messageId) => projection.messageOrder.indexOf(messageId))
    .filter((index) => index >= 0);
  if (anchorIndexes.length > 0) {
    const adjacent = users
      .map((message) => ({
        message,
        distance: Math.min(...anchorIndexes.map((index) => (
          Math.abs(projection.messageOrder.indexOf(message.id) - index)
        ))),
      }))
      .filter(({ distance }) => distance === 1)
      .sort((left, right) => rank(right.message) - rank(left.message));
    const adjacentUser = choose(adjacent.map(({ message }) => message));
    if (adjacentUser) return adjacentUser;
  }

  // Durable failure/activity projections can arrive as a separate turn with
  // no messageIds. In that case the nearest preceding user turn is the only
  // replayable input identity and is preferable to a dead-end retry button.
  const turnIndex = projection.turnOrder.indexOf(turnId);
  if (turnIndex > 0) {
    for (let index = turnIndex - 1; index >= 0; index -= 1) {
      const prior = projection.turnsById[projection.turnOrder[index]!];
      if (!prior) continue;
      const priorUser = choose(prior.messageIds
        .map((messageId) => projection.messagesById[messageId])
        .filter((message): message is AgentMessageProjection => message?.role === 'user'));
      if (priorUser) return priorUser;
    }
  }

  return [...users]
    .filter((message) => message.createdAtMs <= turn.updatedAtMs && meaningful(message))
    .sort((left, right) => right.createdAtMs - left.createdAtMs)[0];
}


function normalizedQueueText(value: string): string {
  return value.replace(/\s+/gu, ' ').trim();
}

function upsertActivity(
  state: AgentProjectionState,
  event: UiAgentEvent,
  payload: Record<string, unknown>,
  status: AgentActivityProjection['status'],
): void {
  const id =
    text(payload.toolCallId ?? payload.approvalId ?? payload.requestId) ||
    `${event.turnId}:${event.eventType}`;
  const previous = state.activitiesById[id];
  if (previous && previous.turnId !== event.turnId) {
    const previousTurn = writableTurn(state, previous.turnId);
    if (previousTurn) {
      previousTurn.activityIds = previousTurn.activityIds.filter((activityId) => activityId !== id);
    }
  }
  const activityPayload = (
    previous
    && (event.eventType === 'approval_required' || event.eventType === 'approval_resolved')
  )
    ? { ...previous.payload, ...payload }
    : mergeActivityPayload(previous, event, payload, status);
  const activity: AgentActivityProjection = {
    id,
    turnId: event.turnId,
    kind: event.eventType,
    status,
    summary: activitySummary(activityPayload, event.eventType),
    payload: activityPayload,
    createdAtMs: previous?.createdAtMs ?? event.createdAtMs,
    updatedAtMs: event.createdAtMs,
    timelineSequence: previous?.timelineSequence ?? sourceTimelineSequence(event),
  };
  if (!previous) state.activityOrder.push(id);
  state.activitiesById[id] = activity;
  updateAgentTodoFromActivity(state, activityPayload);
  const turn = ensureTurn(state, event.turnId, event.createdAtMs);
  if (!turn.activityIds.includes(id)) turn.activityIds.push(id);
}

function upsertApprovalActivity(
  state: AgentProjectionState,
  event: UiAgentEvent,
  payload: Record<string, unknown>,
  status: AgentActivityProjection['status'],
): void {
  const toolCallId = text(payload.toolCallId);
  if (!toolCallId) {
    upsertActivity(state, event, payload, status);
    return;
  }
  const owner = state.activitiesById[toolCallId];
  if (
    !owner
    && payload.automatic === true
    && status === 'completed'
  ) {
    // A bounded journal may retain the policy receipt after its Tool owner has
    // fallen out of the window. The successful automatic receipt adds no
    // user action or result of its own, so do not manufacture a Room-root
    // conversation turn for it. Failed/denied receipts remain visible.
    return;
  }
  upsertActivity(
    state,
    {
      ...event,
      turnId: owner?.turnId || event.turnId,
      payload: { ...payload, toolCallId },
    },
    payload,
    status,
  );
  if (owner?.kind.startsWith('tool_')) {
    const correlated = state.activitiesById[toolCallId];
    if (correlated) {
      state.activitiesById[toolCallId] = {
        ...correlated,
        kind: owner.kind,
      };
    }
  }
}

function roomBoundApprovalToolOwner(
  state: AgentProjectionState,
  payload: Record<string, unknown>,
): AgentActivityProjection | undefined {
  const owner = state.activitiesById[text(payload.toolCallId)];
  if (!owner?.kind.startsWith('tool_')) return undefined;
  const causal = record(payload.causalMetadata ?? owner.payload.causalMetadata);
  return causal.roomBound === true
    && Boolean(text(causal.roomId))
    && Boolean(text(causal.rootId))
    && Boolean(text(causal.dispatchId))
    ? owner
    : undefined;
}

function approvalActivityTurnId(
  state: AgentProjectionState,
  payload: Record<string, unknown>,
  fallbackTurnId: string,
): string {
  const owner = state.activitiesById[text(payload.toolCallId)];
  return owner?.turnId || fallbackTurnId;
}

function mergeLegacyApprovalIntoTool(
  state: AgentProjectionState,
  event: UiAgentEvent,
  payload: Record<string, unknown>,
): Record<string, unknown> {
  const toolCallId = text(payload.toolCallId);
  const approvalId = approvalIdFromActivityPayload(payload);
  if (!toolCallId || !approvalId || toolCallId === approvalId) return payload;
  const legacyApproval = state.activitiesById[approvalId];
  if (!legacyApproval || !legacyApproval.kind.includes('approval')) return payload;

  const legacyTurn = writableTurn(state, legacyApproval.turnId);
  if (legacyTurn) {
    legacyTurn.activityIds = legacyTurn.activityIds.filter((id) => id !== approvalId);
  }
  delete state.activitiesById[approvalId];
  state.activityOrder = state.activityOrder.filter((id) => id !== approvalId);
  if (
    legacyTurn
    && legacyApproval.turnId !== event.turnId
    && legacyTurn.activityIds.length === 0
    && legacyTurn.messageIds.length === 0
  ) {
    delete state.turnsById[legacyApproval.turnId];
    state.turnOrder = state.turnOrder.filter((id) => id !== legacyApproval.turnId);
  }
  return { ...legacyApproval.payload, ...payload };
}

function approvalIdFromActivityPayload(
  payload: Record<string, unknown>,
): string {
  const result = record(payload.result);
  const publicResult = record(payload.publicResult);
  const details = record(result.details);
  const layers = [
    payload,
    publicResult,
    result,
    details,
    record(details.result),
    record(result.result),
  ];
  for (const layer of layers) {
    const approvalId = text(layer.approvalId);
    if (approvalId) return approvalId;
  }
  return '';
}



function mergeActivityPayload(
  previous: AgentActivityProjection | undefined,
  event: UiAgentEvent,
  payload: Record<string, unknown>,
  status: AgentActivityProjection['status'],
): Record<string, unknown> {
  if (!isToolActivityEvent(event.eventType)) return payload;

  const previousPayload = previous?.payload ?? {};
  const history = agentToolProgressHistory(previousPayload.progressHistory);
  const nextEntry: AgentToolProgressEntry = {
    eventId: event.eventId,
    kind: event.eventType,
    status,
    summary: toolProgressSummary(payload, previousPayload, event.eventType, status),
    createdAtMs: event.createdAtMs,
  };
  const progressHistory = history.some((entry) => entry.eventId === event.eventId)
    ? history
    : [...history, nextEntry].slice(-20);

  // These events are updates for one logical tool call. Keep stable metadata
  // and prior partial results while the latest event advances its status.
  const merged: Record<string, unknown> = {
    ...previousPayload,
    ...payload,
    progressHistory,
  };
  if (
    Object.keys(record(payload.args)).length === 0
    && Object.keys(record(previousPayload.args)).length > 0
  ) {
    merged.args = previousPayload.args;
  }
  return merged;
}

export function agentToolProgressHistory(value: unknown): AgentToolProgressEntry[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item): AgentToolProgressEntry[] => {
    const entry = record(item);
    const kind = text(entry.kind);
    const status = text(entry.status);
    if (
      !['tool_started', 'tool_progress', 'tool_finished'].includes(kind)
      || !['running', 'waiting', 'completed', 'failed'].includes(status)
    ) return [];
    return [{
      eventId: text(entry.eventId),
      kind: kind as AgentToolProgressEntry['kind'],
      status: status as AgentToolProgressEntry['status'],
      summary: boundedToolProgressText(entry.summary),
      createdAtMs: finiteTimestamp(entry.createdAtMs),
    }];
  }).filter((entry) => entry.eventId && entry.createdAtMs > 0);
}

function toolProgressSummary(
  payload: Record<string, unknown>,
  previousPayload: Record<string, unknown>,
  eventType: AgentToolProgressEntry['kind'],
  status: AgentActivityProjection['status'],
): string {
  const carrier = record(payload.result ?? payload.partialResult);
  const details = record(carrier.details);
  const domain = record(details.result ?? carrier.result);
  const explicit = boundedToolProgressText(
    domain.summary
      ?? details.summary
      ?? carrier.summary
      ?? payload.summary
      ?? payload.message
      ?? payload.label,
  );
  if (explicit) return explicit;
  const toolName = boundedToolProgressText(
    payload.toolName ?? payload.toolId ?? previousPayload.toolName ?? previousPayload.toolId,
  ) || '工具';
  if (status === 'failed') return `${toolName}执行失败`;
  if (eventType === 'tool_finished') return `${toolName}执行完成`;
  if (eventType === 'tool_started') return `${toolName}已开始`;
  return `${toolName}正在处理`;
}

function expectedToolNoop(
  payload: Record<string, unknown>,
): { kind: 'act_gate' | 'schema_active'; summary: string } | undefined {
  const carrier = record(payload.result ?? payload.partialResult);
  const details = record(carrier.details);
  const domain = record(details.result ?? carrier.result);
  const content = Array.isArray(carrier.content)
    ? carrier.content.map((item) => text(record(item).text))
    : [];
  const messages = [
    payload.error,
    payload.summary,
    details.error,
    details.summary,
    domain.error,
    domain.summary,
    ...content,
  ].filter((value): value is string => typeof value === 'string');
  if (messages.some((value) => value.startsWith('Act Gate blocked workspace mutation ('))) {
    return {
      kind: 'act_gate',
      summary: '工作区变更未执行：当前 Todo、Goal 或权限状态不允许执行。',
    };
  }
  if (messages.some((value) => value.startsWith('Tool schema is already active;'))) {
    return {
      kind: 'schema_active',
      summary: '工具已经可直接调用，无需重复加载。',
    };
  }
  return undefined;
}

function boundedToolProgressText(value: unknown): string {
  if (typeof value !== 'string' && typeof value !== 'number' && typeof value !== 'boolean') return '';
  const normalized = String(value)
    .replace(/\s+/g, ' ')
    .replace(/(?:\/Users|\/Volumes|\/private|\/tmp)\/[^\s,;，。]+/g, '本地资源')
    .replace(/(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[^\s,;]+/gi, '敏感信息已隐藏')
    .trim();
  return normalized.length > 240 ? `${normalized.slice(0, 240)}…` : normalized;
}

function finiteTimestamp(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function isToolActivityEvent(value: string): value is AgentToolProgressEntry['kind'] {
  return value === 'tool_started' || value === 'tool_progress' || value === 'tool_finished';
}

function activitySummary(payload: Record<string, unknown>, fallback: string): string {
  return text(payload.summary ?? payload.label ?? payload.message ?? payload.toolName) || fallback;
}

function touchTurn(
  state: AgentProjectionState,
  turnId: string,
  status: AgentTurnStatus,
  nowMs: number,
): void {
  const turn = ensureTurn(state, turnId, nowMs);
  turn.status = status;
  turn.updatedAtMs = nowMs;
}

/** A Provider transport failure can be persisted as a failed assistant
 * message before Pi's retry continues the same logical turn. Later reasoning,
 * text or Tool work proves that message was an attempt failure, not the turn's
 * terminal fence. Reopen only failures without an authoritative turn_failed
 * activity so a genuinely terminal turn cannot be revived by a late receipt. */
function reopenProvisionalTurn(
  state: AgentProjectionState,
  turnId: string,
  nowMs: number,
): void {
  if (!turnId) return;
  const turn = ensureTurn(state, turnId, nowMs);
  const hasTerminalFailure = turn.activityIds.some(
    (activityId) => state.activitiesById[activityId]?.kind === 'turn_failed',
  );
  if (turn.status === 'failed' && hasTerminalFailure) return;
  turn.status = 'running';
  turn.updatedAtMs = nowMs;
  delete turn.failure;
}

function completeTurn(
  state: AgentProjectionState,
  turnId: string,
  status: Extract<AgentTurnStatus, 'completed' | 'failed' | 'aborted'>,
  nowMs: number,
  failure = '',
  settleCompletedActivities = false,
): void {
  const turn = ensureTurn(state, turnId, nowMs);
  turn.status = status;
  turn.updatedAtMs = nowMs;
  if (failure) turn.failure = failure;
  else if (status !== 'failed') delete turn.failure;
  if (status !== 'failed') {
    const supersededFailureIds = turn.activityIds.filter(
      (activityId) => state.activitiesById[activityId]?.kind === 'turn_failed',
    );
    if (supersededFailureIds.length > 0) {
      const superseded = new Set(supersededFailureIds);
      turn.activityIds = turn.activityIds.filter((activityId) => !superseded.has(activityId));
      state.activityOrder = state.activityOrder.filter((activityId) => !superseded.has(activityId));
      for (const activityId of supersededFailureIds) delete state.activitiesById[activityId];
    }
  }
  /*
   * Settle activities the turn never finished. Without this a stopped or
   * failed turn keeps rendering its in-flight tools as "running" forever:
   * the turn header reads 已停止 while the tool row underneath still reads
   * 进行中, and its elapsed timer keeps counting. A completed turn is left
   * alone — a tool that is genuinely still running there is real state.
   */
  if (status !== 'completed' || settleCompletedActivities) {
    for (const activityId of turn.activityIds) {
      const activity = state.activitiesById[activityId];
      if (!activity || !['running', 'waiting'].includes(activity.status)) continue;
      state.activitiesById[activityId] = {
        ...activity,
        status: status === 'failed' ? 'failed' : 'completed',
        settledByTurnStatus: status,
        updatedAtMs: nowMs,
      };
    }
  }
  for (const messageId of turn.messageIds) {
    const message = state.messagesById[messageId];
    if (!message) continue;
    const messageStatus = status === 'completed' ? 'completed' : status;
    state.messagesById[messageId] = {
      ...message,
      status: messageStatus,
      blocks: message.blocks.map((block) => ({
        ...block,
        /* A queued block never started; once the turn is over it cannot still
           be waiting its turn. Settling only `running` left finished turns
           displaying tools as 排队中 forever — the same stale-state defect the
           activity list had. Aborted turns settle their pending work to
           completed rather than failed: the user stopped it, it did not break. */
        status: block.status === 'running' || block.status === 'queued'
          ? (status === 'aborted' ? 'completed' : messageStatus)
          : block.status,
      })),
      completedAtMs: nowMs,
    };
  }
}

function ensureTurn(
  state: AgentProjectionState,
  requestedTurnId: string,
  nowMs: number,
): AgentTurnProjection {
  const turnId = requestedTurnId || 'unscoped';
  let turn = writableTurn(state, turnId);
  if (!turn) {
    turn = {
      id: turnId,
      status: 'running',
      messageIds: [],
      activityIds: [],
      createdAtMs: nowMs,
      updatedAtMs: nowMs,
    };
    state.turnsById[turnId] = turn;
    state.turnOrder.push(turnId);
  }
  return turn;
}

function attachMessageToTurn(state: AgentProjectionState, message: UiAgentMessage): void {
  const turn = ensureTurn(state, message.turnId, message.createdAtMs);
  if (!turn.messageIds.includes(message.id)) turn.messageIds.push(message.id);
  turn.updatedAtMs = Math.max(turn.updatedAtMs, message.completedAtMs ?? message.createdAtMs);
}

function reconcileSnapshotTurnStatuses(
  state: AgentProjectionState,
  admissionsRestored = true,
): void {
  const lastTurnId = state.turnOrder[state.turnOrder.length - 1] ?? '';
  const runtimeStatus = turnStatusFromRuntime(state.status);
  for (const turnId of state.turnOrder) {
    const turn = writableTurn(state, turnId);
    if (!turn) continue;
    const messages = turn.messageIds
      .map((messageId) => state.messagesById[messageId])
      .filter((message): message is UiAgentMessage => Boolean(message));
    // A failed Provider attempt remains in the transcript after Pi retries.
    // The latest assistant message determines the attempt outcome; retaining
    // the earlier error must not turn a later successful answer into failure.
    const latestAssistant = messages.filter((message) => message.role === 'assistant').at(-1);
    const statuses = new Set(messages
      .filter((message) => message.role !== 'assistant' || message === latestAssistant)
      .map((message) => message.status));
    const retryActive = turn.activityIds.some((id) => {
      const activity = state.activitiesById[id];
      return activity?.kind === 'status_changed'
        && activity.payload.phase === 'provider_retry'
        && activity.status === 'running';
    });
    const terminalFailure = turn.activityIds.some((id) => state.activitiesById[id]?.kind === 'turn_failed');
    if (retryActive && !terminalFailure && runtimeStatus === 'running') {
      turn.status = 'running';
      delete turn.failure;
    }
    else if (statuses.has('failed')) turn.status = 'failed';
    else if (statuses.has('streaming')) turn.status = 'running';
    else if (statuses.has('queued')) {
      turn.status = messages.some((message) => (
        message.admissionState === 'pending'
        || message.admissionState === 'unresolved'
      )) ? 'waiting' : 'queued';
    }
    else if (statuses.has('aborted')) turn.status = 'aborted';
    else if (turn.activityIds.some((id) => state.activitiesById[id]?.status === 'waiting')) turn.status = 'waiting';
    else if (turn.activityIds.some((id) => state.activitiesById[id]?.status === 'running')) turn.status = 'running';
    else if (runtimeStatus === 'failed'
      && turn.activityIds.some((id) => state.activitiesById[id]?.status === 'failed')
      && !messages.some((message) => message.role === 'assistant' && message.status === 'completed')) turn.status = 'failed';
    else turn.status = 'completed';

    const hasUserMessage = messages.some((message) => message.role === 'user');
    const hasAssistantMessage = messages.some((message) => message.role === 'assistant');
    const hasTerminalActivity = turn.activityIds.some((activityId) => {
      const activity = state.activitiesById[activityId];
      return activity?.status === 'completed' || activity?.status === 'failed';
    });
    const activeTail = turnId === lastTurnId && runtimeStatus !== 'completed';
    if (
      admissionsRestored
      && turn.status === 'completed'
      && hasUserMessage
      && !hasAssistantMessage
      && !hasTerminalActivity
      && !activeTail
    ) {
      turn.status = 'failed';
      turn.failure = turn.failure || '未收到助手回复。';
    }
  }

  const lastTurn = writableTurn(state, lastTurnId);
  if (lastTurn && runtimeStatus !== 'completed' && lastTurn.status === 'completed') {
    lastTurn.status = runtimeStatus;
  }
}

function detachMessageFromTurn(state: AgentProjectionState, message: UiAgentMessage): void {
  const turn = writableTurn(state, message.turnId);
  if (!turn) return;
  turn.messageIds = turn.messageIds.filter((id) => id !== message.id);
  if (turn.messageIds.length === 0 && turn.activityIds.length === 0) {
    delete state.turnsById[turn.id];
    state.turnOrder = state.turnOrder.filter((id) => id !== turn.id);
  }
}

function appendDiagnostic(state: AgentProjectionState, diagnostic: ProjectionDiagnostic): void {
  state.diagnostics.push(diagnostic);
  if (state.diagnostics.length > diagnosticLimit) {
    state.diagnostics.splice(0, state.diagnostics.length - diagnosticLimit);
  }
}

function snapshotFromEvent(event: UiAgentEvent, fallbackSequence: number): AgentSnapshot {
  const payload = record(event.payload);
  const snapshot = agentSnapshotFromResponse(payload.snapshot ?? payload);
  return {
    ...snapshot,
    lastSequence: snapshot.lastSequence || event.sequence || fallbackSequence,
    resumeToken: snapshot.resumeToken || event.resumeToken,
  };
}

function parseBackgroundJob(value: unknown): AgentBackgroundJobV1 | undefined {
  const parsed = validateContract('agent-background-job.v1', value);
  return parsed.ok ? parsed.value : undefined;
}

// Timestamps remain authoritative across retry attempts; this order only
// breaks equal-time ties so terminal states cannot regress to active states.
const BACKGROUND_JOB_STATUS_PRECEDENCE: Readonly<
  Record<AgentBackgroundJobV1['status'], number>
> = {
  queued: 0,
  running: 1,
  cancelling: 2,
  completed: 3,
  failed: 4,
  cancelled: 5,
  orphaned: 6,
};

function upsertBackgroundJob(
  state: AgentProjectionState,
  job: AgentBackgroundJobV1,
): void {
  state.backgroundJobsById[job.jobId] = job;
  if (!state.backgroundJobOrder.includes(job.jobId)) {
    state.backgroundJobOrder = [job.jobId, ...state.backgroundJobOrder];
  }
}

function parseLifecycleCancellationAudit(
  value: unknown,
): AgentLifecycleCancellationAuditV1 | undefined {
  const parsed = validateContract('agent-lifecycle-cancellation-audit.v1', value);
  return parsed.ok ? parsed.value : undefined;
}

function upsertLifecycleCancellationAudit(
  state: AgentProjectionState,
  audit: AgentLifecycleCancellationAuditV1,
): void {
  const current = state.lifecycleCancellationAuditsById[audit.requestId];
  if (current && current.updatedAtMs > audit.updatedAtMs) return;
  state.lifecycleCancellationAuditsById[audit.requestId] = audit;
  state.lifecycleCancellationAuditOrder = [
    audit.requestId,
    ...state.lifecycleCancellationAuditOrder.filter((requestId) => requestId !== audit.requestId),
  ].sort((leftId, rightId) => (
    (state.lifecycleCancellationAuditsById[rightId]?.updatedAtMs ?? 0)
    - (state.lifecycleCancellationAuditsById[leftId]?.updatedAtMs ?? 0)
  ));
}

function turnStatusFromRuntime(status: string): AgentTurnStatus {
  if (status === 'waiting') return 'waiting';
  if (status === 'failed' || status === 'faulted') return 'failed';
  if (status === 'idle' || status === 'ready' || status === 'stopped' || status === 'active') {
    return 'completed';
  }
  return 'running';
}

function cloneState(state: AgentProjectionState): AgentProjectionState {
  return {
    ...state,
    messagesById: { ...state.messagesById },
    messageOrder: [...state.messageOrder],
    // Turn values are copied only when a reducer writes to them. Streaming
    // deltas therefore keep every completed turn referentially stable instead
    // of cloning the whole conversation on every token batch.
    turnsById: { ...state.turnsById },
    turnOrder: [...state.turnOrder],
    activitiesById: { ...state.activitiesById },
    activityOrder: [...state.activityOrder],
    optimisticByClientMessageId: { ...state.optimisticByClientMessageId },
    diagnostics: [...state.diagnostics],
    backgroundJobsById: { ...state.backgroundJobsById },
    backgroundJobOrder: [...state.backgroundJobOrder],
    lifecycleCancellationAuditsById: { ...state.lifecycleCancellationAuditsById },
    lifecycleCancellationAuditOrder: [...state.lifecycleCancellationAuditOrder],
    messageQueue: {
      steering: [...state.messageQueue.steering],
      followUp: [...state.messageQueue.followUp],
    },
    todo: {
      ...state.todo,
      phases: state.todo.phases.map((phase) => ({
        ...phase,
        tasks: phase.tasks.map((task) => ({ ...task })),
      })) as AgentTodoProjection['phases'],
      counts: { ...state.todo.counts },
    },
    goal: {
      ...state.goal,
      budget: { ...state.goal.budget },
      usage: { ...state.goal.usage },
      remaining: { ...state.goal.remaining },
      completionAudit: state.goal.completionAudit
        ? { ...state.goal.completionAudit, evidence: state.goal.completionAudit.evidence }
        : null,
    },
    actGate: { ...state.actGate },
    ...(state.gap ? { gap: { ...state.gap } } : {}),
  };
}

function canStartAgentDeltaBatch(
  state: AgentProjectionState,
  event: UiAgentEvent,
): boolean {
  return (
    event.eventType === 'text_delta'
    && event.sessionId === state.sessionId
    && !state.needsSnapshot
    && event.sequence > state.lastSequence
    && (state.lastSequence === 0 || event.sequence === state.lastSequence + 1)
  );
}

function canMergeAgentDelta(
  previous: UiAgentEvent,
  candidate: UiAgentEvent,
): boolean {
  if (
    candidate.eventType !== 'text_delta'
    || candidate.sessionId !== previous.sessionId
    || candidate.turnId !== previous.turnId
    || candidate.sequence !== previous.sequence + 1
    || candidate.payload.replaceBlock === true
  ) {
    return false;
  }
  return sameDeltaField(previous.payload, candidate.payload, 'messageId')
    && sameDeltaField(previous.payload, candidate.payload, 'blockId')
    && sameDeltaField(previous.payload, candidate.payload, 'contentIndex');
}

function sameDeltaField(
  left: Record<string, unknown>,
  right: Record<string, unknown>,
  key: string,
): boolean {
  return left[key] === right[key];
}

function writableTurn(
  state: AgentProjectionState,
  turnId: string,
): AgentTurnProjection | undefined {
  const current = state.turnsById[turnId];
  if (!current) return undefined;
  const copy = {
    ...current,
    messageIds: [...current.messageIds],
    activityIds: [...current.activityIds],
  };
  state.turnsById[turnId] = copy;
  return copy;
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function parseMessageQueue(value: unknown): AgentMessageQueue {
  const source = record(value);
  return {
    steering: Array.isArray(source.steering)
      ? source.steering.filter((item): item is string => typeof item === 'string')
      : [],
    followUp: Array.isArray(source.followUp)
      ? source.followUp.filter((item): item is string => typeof item === 'string')
      : [],
  };
}

function emptyAgentTodo(sessionId: string): AgentTodoProjection {
  return {
    schemaVersion: 'rag-ime.agent-todo.v1',
    id: `todo:${sessionId}`,
    sessionId,
    revision: 0,
    actor: '',
    updatedAtMs: 0,
    roomLineage: null,
    phases: [],
    counts: {
      total: 0,
      pending: 0,
      inProgress: 0,
      blocked: 0,
      completed: 0,
      abandoned: 0,
    },
  };
}

export function parseAgentTodo(value: unknown): AgentTodoProjection | undefined {
  const source = record(value);
  if (
    source.schemaVersion !== 'rag-ime.agent-todo.v1'
    || !Array.isArray(source.phases)
  ) return undefined;
  const phases = source.phases.slice(0, 16).flatMap((rawPhase) => {
    const phase = record(rawPhase);
    const name = text(phase.name).replace(/\s+/g, ' ').trim().slice(0, 80);
    if (!name || !Array.isArray(phase.tasks)) return [];
    const tasks = phase.tasks.slice(0, 100).flatMap((rawTask) => {
      const task = record(rawTask);
      const content = text(task.content).replace(/\s+/g, ' ').trim().slice(0, 240);
      const status = text(task.status);
      const reason = text(task.reason).replace(/\s+/g, ' ').trim().slice(0, 500);
      if (
        !content
        || !['pending', 'in_progress', 'blocked', 'completed', 'abandoned'].includes(status)
      ) return [];
      return [{ content, status, ...(reason ? { reason } : {}) }];
    });
    return [{ name, tasks }];
  }) as AgentTodoProjection['phases'];
  const tasks = phases.flatMap((phase) => phase.tasks);
  const pending = tasks.filter((task) => task.status === 'pending').length;
  const inProgress = tasks.filter((task) => task.status === 'in_progress').length;
  const blocked = tasks.filter((task) => task.status === 'blocked').length;
  const completed = tasks.filter((task) => task.status === 'completed').length;
  const abandoned = tasks.filter((task) => task.status === 'abandoned').length;
  return {
    schemaVersion: 'rag-ime.agent-todo.v1',
    id: text(source.id) || `todo:${text(source.sessionId)}`,
    sessionId: text(source.sessionId),
    revision: integer(source.revision),
    actor: text(source.actor).slice(0, 120),
    updatedAtMs: integer(source.updatedAtMs),
    roomLineage: parseRoomTodoLineage(source.roomLineage),
    phases,
    counts: {
      total: tasks.length,
      pending,
      inProgress,
      blocked,
      completed,
      abandoned,
    },
  };
}

function parseRoomTodoLineage(
  value: unknown,
): AgentTodoProjection['roomLineage'] {
  if (value === null || value === undefined) return null;
  const source = record(value);
  const textFields = [
    'roomId',
    'rootId',
    'taskId',
    'workItemId',
    'dispatchId',
    'sessionId',
    'participantId',
  ] as const;
  if (
    source.schemaVersion !== 'wisdom-weasel.room-todo-lineage.v1'
    || textFields.some((field) => !text(source[field]).trim())
  ) return null;
  return {
    schemaVersion: 'wisdom-weasel.room-todo-lineage.v1',
    roomId: text(source.roomId).trim(),
    rootId: text(source.rootId).trim(),
    taskId: text(source.taskId).trim(),
    workItemId: text(source.workItemId).trim(),
    dispatchId: text(source.dispatchId).trim(),
    sessionId: text(source.sessionId).trim(),
    participantId: text(source.participantId).trim(),
    generation: integer(source.generation),
    taskRevision: integer(source.taskRevision),
    ownershipRevision: integer(source.ownershipRevision),
    workItemRevision: integer(source.workItemRevision),
  };
}

function emptyAgentGoal(): AgentGoalProjection {
  return {
    schemaVersion: 'rag-ime.agent-goal.v1',
    sessionId: '',
    configured: false,
    goalId: '',
    revision: 0,
    objective: '',
    successCriteria: '',
    evidenceExpectations: [],
    status: 'cleared',
    budget: { tokenLimit: null, timeLimitMs: null },
    usage: { tokens: 0, elapsedMs: 0 },
    remaining: { tokens: null, timeMs: null },
    budgetExceeded: false,
    completionAudit: null,
    cancellationAudit: null,
    updatedAtMs: 0,
  };
}

function closedActGate(): AgentActGateProjection {
  return {
    allowed: true,
    reason: 'user_execution_request',
    message: '用户的执行请求允许在已授权工作区内继续。',
    todoRevision: 0,
    goalRevision: 0,
  };
}

function parseAgentGoal(value: unknown): AgentGoalProjection | undefined {
  const source = record(value);
  if (source.schemaVersion !== 'rag-ime.agent-goal.v1') return undefined;
  const status = text(source.status);
  if (!['active', 'paused', 'completed', 'cancelled', 'cleared'].includes(status)) return undefined;
  const budget = record(source.budget);
  const usage = record(source.usage);
  const remaining = record(source.remaining);
  const auditSource = record(source.completionAudit);
  const cancellationSource = record(source.cancellationAudit);
  const evidence = Array.isArray(auditSource.evidence)
    ? auditSource.evidence.flatMap((rawEvidence) => {
      const item = record(rawEvidence);
      const kind = text(item.kind);
      const summary = text(item.summary);
      const reference = text(item.reference);
      return ['test', 'artifact', 'commit', 'receipt', 'note'].includes(kind) && summary && reference
        ? [{ kind, summary, reference }]
        : [];
    })
    : [];
  return {
    schemaVersion: 'rag-ime.agent-goal.v1',
    sessionId: text(source.sessionId),
    configured: source.configured === true,
    goalId: text(source.goalId),
    revision: integer(source.revision),
    objective: text(source.objective).slice(0, 4_000),
    successCriteria: text(source.successCriteria).slice(0, 2_000),
    evidenceExpectations: (Array.isArray(source.evidenceExpectations)
      ? source.evidenceExpectations
        .map((item) => text(item).trim().slice(0, 600))
        .filter(Boolean)
        .slice(0, 20)
      : []) as AgentGoalProjection['evidenceExpectations'],
    status: status as AgentGoalProjection['status'],
    budget: {
      tokenLimit: nullableInteger(budget.tokenLimit),
      timeLimitMs: nullableInteger(budget.timeLimitMs),
    },
    usage: { tokens: integer(usage.tokens), elapsedMs: integer(usage.elapsedMs) },
    remaining: {
      tokens: nullableInteger(remaining.tokens),
      timeMs: nullableInteger(remaining.timeMs),
    },
    budgetExceeded: source.budgetExceeded === true,
    completionAudit: auditSource.auditId && evidence.length
      ? {
          auditId: text(auditSource.auditId),
          summary: text(auditSource.summary),
          evidence: evidence as NonNullable<AgentGoalProjection['completionAudit']>['evidence'],
          completedBy: text(auditSource.completedBy),
          createdAtMs: integer(auditSource.createdAtMs),
        }
      : null,
    cancellationAudit: cancellationSource.auditId && cancellationSource.reason
      ? {
          auditId: text(cancellationSource.auditId),
          reason: text(cancellationSource.reason).slice(0, 1_000),
          cancelledBy: text(cancellationSource.cancelledBy).slice(0, 120),
          createdAtMs: integer(cancellationSource.createdAtMs),
        }
      : null,
    updatedAtMs: integer(source.updatedAtMs),
  };
}

function parseActGate(
  value: unknown,
  fallbackTodoRevision = 0,
  fallbackGoalRevision = 0,
): AgentActGateProjection | undefined {
  const source = record(value);
  const reason = text(source.reason);
  if (![
    'approved',
    'user_execution_request',
    'goal_paused',
    'goal_completed',
    'goal_budget_exhausted',
    'goal_cancelled',
  ].includes(reason)) return undefined;
  return {
    allowed: source.allowed === true,
    reason: reason as AgentActGateProjection['reason'],
    message: text(source.message),
    todoRevision: source.todoRevision === undefined
      ? fallbackTodoRevision
      : integer(source.todoRevision),
    goalRevision: source.goalRevision === undefined
      ? fallbackGoalRevision
      : integer(source.goalRevision),
  };
}

function nullableInteger(value: unknown): number | null {
  return value === null ? null : integer(value);
}

function updateAgentTodoFromActivity(
  state: AgentProjectionState,
  payload: Record<string, unknown>,
): void {
  const toolId = text(payload.toolId ?? payload.toolName);
  if (toolId !== 'todo') return;
  const carrier = record(payload.result ?? payload.partialResult);
  const details = record(carrier.details);
  const candidates = [
    record(record(details.result).todo),
    record(record(carrier.result).todo),
    record(details.todo),
    record(carrier.todo),
    record(payload.todo),
    record(details.result),
    record(carrier.result),
    details,
    carrier,
    payload,
  ];
  for (const candidate of candidates) {
    const todo = parseAgentTodo(candidate);
    if (!todo) continue;
    state.todo = todo;
    return;
  }
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function sourceTimelineSequence(event: UiAgentEvent): number {
  return typeof event.timelineSequence === 'number'
    && Number.isFinite(event.timelineSequence)
    ? event.timelineSequence
    : event.sequence;
}

function integer(value: unknown): number {
  return typeof value === 'number' && Number.isInteger(value) ? Math.max(0, value) : 0;
}
