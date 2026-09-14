import type { ProjectionDiagnostic, ProjectionGap, ProjectionReduction } from './agent-reducer';
import { approvalNeedsHumanDecision } from './approval-decision';
import {
  MAX_COMPOSER_ATTACHMENT_BYTES,
  isComposerAttachmentMimeType,
  isComposerImageMimeType,
} from './attachment-policy';
import type { AgentRoomConversationSnapshotV1, AgentRoomEventPageV1, AgentRoomSnapshotV1, RoomPostV2 } from './generated';
import type { UiAgentMessage, UiRoomEvent } from './ui-events';
import { parseContract, parseRoomEvent, tryParseAgentMessage } from './validators';

export interface RoomMessageQuestionProjection {
  prompt: string;
  options: RoomQuestionOptionProjection[];
  status: 'pending' | 'answered' | 'superseded';
  answer?: string;
}

export interface RoomMessageProjection {
  id: string;
  roomId: string;
  turnId: string;
  participantId: string | null;
  sourceSessionId: string;
  role: 'user' | 'assistant';
  status: 'queued' | 'streaming' | 'completed' | 'failed' | 'aborted';
  text: string;
  message?: UiAgentMessage;
  clientMessageId?: string;
  projectionKind?: 'optimistic' | 'execution' | 'post';
  rootId?: string;
  dispatchId?: string;
  sourceMessageId?: string;
  sourceBlockId?: string;
  sourceEventId?: string;
  /** Persisted human identity for Team Room user posts. */
  actorUserId?: string;
  actorDisplayName?: string;
  /** Opaque Pi Session turn identity; may contain several assistant/tool loops. */
  sourceTurnId?: string;
  /** Opaque Pi assistant/tool loop identity; exactly one Room card boundary. */
  sourceLoopId?: string;
  /** Authoritative server event order; optimistic messages fall back to time. */
  sequence?: number;
  /** Canonical RoomPost order metadata when the publication carries it. */
  chronology?: RoomPostV2['chronology'];
  createdAtMs: number;
  postKind?: RoomPostV2['kind'];
  mentionedParticipantIds?: string[];
  question?: RoomMessageQuestionProjection;
  answerToPostId?: string;
  /** Authoritative link to the failed/aborted Root this attempt retries. */
  retryOfRootId?: string;
  completedAtMs?: number;
}

export type RoomQuestionOptionProjection =
  NonNullable<RoomPostV2['question']>['options'][number];

export interface PendingRoomQuestionProjection {
  postId: string;
  roomId: string;
  rootId: string;
  sequence: number;
  prompt: string;
  options: RoomQuestionOptionProjection[];
}

export interface RoomActivityProjection {
  id: string;
  /** Authoritative Room event sequence used to preserve cross-lane chronology. */
  sequence?: number;
  turnId: string;
  participantId: string | null;
  sourceSessionId: string;
  kind: string;
  status: 'running' | 'waiting' | 'completed' | 'failed' | 'aborted';
  summary: string;
  payload: Record<string, unknown>;
  createdAtMs: number;
  updatedAtMs?: number;
}

export interface RoomActivityLaneIdentity {
  rootId: string;
  participantId: string;
  dispatchId: string;
  sourceTurnId: string;
  sourceLoopId: string;
  key: string;
}

export type RoomParticipantPublicProgressKind =
  | 'reasoning'
  | 'progress'
  | 'tool'
  | 'dispatch'
  | 'status'
  | 'post'
  | 'activity';

export interface RoomParticipantPublicProgressProjection {
  participantId: string;
  sourceSessionId: string;
  rootId: string;
  dispatchId: string;
  kind: RoomParticipantPublicProgressKind;
  status: RoomActivityProjection['status'];
  summary: string;
  /** Public event metadata used only to name tools and summarize visible work. */
  data?: Record<string, unknown>;
  updatedAtMs: number;
}
export interface RoomTurnProjection {
  id: string;
  /**
   * UI-only identity for a turn that was first rendered optimistically. The
   * Room event stream remains keyed by `id`; this alias lets projections keep
   * the same logical round when the server replaces the local turn id.
   */
  logicalRootId?: string;
  rootId?: string;
  status: 'queued' | 'running' | 'completed' | 'failed' | 'aborted';
  messageIds: string[];
  activityIds: string[];
  participantIds: string[];
  terminalParticipantIds?: string[];
  failedParticipantIds?: string[];
  abortedParticipantIds?: string[];
  dispatchIds?: string[];
  terminalDispatchIds?: string[];
  failedDispatchIds?: string[];
  abortedDispatchIds?: string[];
  dispatchParticipantIds?: Record<string, string>;
  rootTerminalAtMs?: number;
  createdAtMs: number;
  updatedAtMs: number;
  failure?: string;
  retryOfRootId?: string;
}

export interface RoomProjectionState {
  roomId: string;
  /**
   * Authoritative Room moderator identity. This is populated from a replayed
   * Room snapshot when available; legacy event-only projections intentionally
   * leave it absent and trust the typed result post.
   */
  moderatorParticipantId?: string;
  lastSequence: number;
  lastEventId: string;
  resumeToken: string;
  needsSnapshot: boolean;
  gap?: ProjectionGap;
  messagesById: Record<string, RoomMessageProjection>;
  messageOrder: string[];
  activitiesById: Record<string, RoomActivityProjection>;
  activityOrder: string[];
  turnsById: Record<string, RoomTurnProjection>;
  turnOrder: string[];
  optimisticByClientMessageId: Record<string, string>;
  diagnostics: ProjectionDiagnostic[];
  pendingUserQuestion?: PendingRoomQuestionProjection;
}

export interface RoomSnapshot {
  messages: RoomMessageProjection[];
  lastSequence: number;
  resumeToken: string;
}

export type RoomEventSnapshot = Omit<AgentRoomSnapshotV1, 'events'> & {
  events: UiRoomEvent[];
};

export type RoomConversationSnapshot = Omit<
AgentRoomConversationSnapshotV1,
'room' | 'events'
> & {
  room: AgentRoomSnapshotV1['room'];
  events: UiRoomEvent[];
};

export type RoomEventPage = Omit<AgentRoomEventPageV1, 'items'> & {
  items: UiRoomEvent[];
};

export interface RoomEventReductionOptions {
  snapshotReplay?: boolean;
}

export interface RoomAttachmentReceipt {
  mediaId: string;
  roomId: string;
  fileName: string;
  /** Any valid managed-media MIME type; images additionally render thumbnails. */
  mimeType: string;
  byteSize: number;
  sha256: string;
  width?: number | null;
  height?: number | null;
}

export interface OptimisticRoomMessageInput {
  clientMessageId: string;
  text: string;
  nowMs: number;
  attachments?: RoomAttachmentReceipt[];
  answerToPostId?: string;
  retryOfRootId?: string;
}

const diagnosticLimit = 50;

export function createRoomProjection(roomId: string): RoomProjectionState {
  return {
    roomId,
    lastSequence: 0,
    lastEventId: '',
    resumeToken: '',
    needsSnapshot: false,
    messagesById: {},
    messageOrder: [],
    activitiesById: {},
    activityOrder: [],
    turnsById: {},
    turnOrder: [],
    optimisticByClientMessageId: {},
    diagnostics: [],
  };
}

export function reduceRoomEvent(
  state: RoomProjectionState,
  event: UiRoomEvent,
  options: RoomEventReductionOptions = {},
): ProjectionReduction<RoomProjectionState> {
  if (event.roomId !== state.roomId) return { state, disposition: 'ignored-foreign' };
  if (event.sequence <= state.lastSequence) {
    return { state, disposition: 'ignored-duplicate' };
  }
  if (state.needsSnapshot) return { state, disposition: 'ignored-snapshot-pending' };
  if (state.lastSequence > 0 && event.sequence !== state.lastSequence + 1) {
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

  // Snapshot replay starts from a private projection and never publishes an
  // intermediate state. Mutate that private accumulator in place so a
  // message-first window at the 2,000-event server bound does not clone every
  // already-replayed message and turn again for each event (quadratic work on
  // Room open). Live SSE reduction keeps its immutable copy-on-write path.
  const next = options.snapshotReplay ? state : cloneState(state);
  next.lastSequence = event.sequence;
  next.lastEventId = event.eventId;
  next.resumeToken = event.resumeToken;
  const payload = publicRoomPayload(event.payload);
  if (isUnroutedParticipantSessionEvent(next, event, payload)) {
    appendDiagnostic(next, {
      id: `${event.eventId}:unrouted-session-event`,
      streamKind: 'room',
      eventType: 'unrouted_participant_session_event',
      summary: 'An ordinary participant Session event was ignored because no Room dispatch owns it.',
      sequence: event.sequence,
      payload,
    });
    return { state: next, disposition: 'applied' };
  }


  if (isExecutionEventAfterRootTerminal(next, event, payload)) {
    if (isAwaitedRoomFinal(next, event, payload)) {
      // A formal Root can be terminal before its moderator's typed final
      // publication is replayed. Admit that one awaited post so the final
      // projection can complete the Root; all other late execution remains
      // fenced and diagnostic.
    } else {
      appendDiagnostic(next, {
        id: `${event.eventId}:after-root-terminal`,
        streamKind: 'room',
        eventType: 'room_event_after_root_terminal',
        summary: 'A late Room execution event was ignored after the Root terminal fence.',
        sequence: event.sequence,
        payload,
      });
      return { state: next, disposition: 'applied' };
    }
  }

  switch (event.eventType) {
    case 'user_message':
      applyUserMessage(next, event, payload);
      break;
    case 'participant_delta':
      applyParticipantDelta(next, event, payload);
      break;
    case 'participant_message':
      applyParticipantMessage(next, event, payload);
      break;
    case 'room_post':
      applyRoomPost(next, event, payload);
      break;
    case 'route_decision':
    case 'participant_status':
    case 'participant_activity':
      // The cancellation owner emits this whole-Root receipt only after all
      // execution surfaces terminate. Individual dispatch terminals may be
      // absent (for example an earlier moderator dispatch before a wake).
      // Consume the same authority in live delivery and snapshot recovery.
      if (
        event.eventType === 'participant_status'
        && text(payload.status) === 'cancellation_applied'
        && event.turnId
        && text(payload.rootId) === event.turnId
        && text(payload.cancellationReceiptId)
        && Array.isArray(payload.pendingTargets)
        && payload.pendingTargets.length === 0
        && !event.participantId
        && !event.sourceSessionId
      ) {
        completeTurn(next, event.turnId, 'aborted', event.createdAtMs);
        break;
      }
      // A temporary subagent also publishes detached lifecycle receipts so
      // its own projection can refresh. The rooted `agents` Tool activity is
      // already present in the Partner lane; turning this duplicate receipt
      // into an `unscoped` Room turn leaves the Room permanently busy after a
      // refresh.
      if (
        event.eventType === 'participant_activity'
        && !event.turnId
        && text(payload.sourceEventType) === 'tool_progress'
        && text(payload.toolCallId).startsWith('subagent:')
      ) break;
      upsertActivity(next, event, payload);
      break;
    case 'turn_completed':
      completeParticipantTurn(
        next,
        event,
        text(payload.dispatchId),
        text(payload.status) === 'aborted' || payload.aborted === true
          ? 'aborted'
          : 'completed',
        event.createdAtMs,
      );
      break;
    case 'turn_failed':
      {
        const failure = [text(payload.error) || text(payload.summary), text(payload.nextStep)]
          .filter(Boolean)
          .join('；');
        completeParticipantTurn(
          next,
          event,
          text(payload.dispatchId),
          'failed',
          event.createdAtMs,
          failure,
        );
        upsertActivity(next, event, payload, 'failed');
        break;
      }
    case 'room_config_changed':
    case 'topic_changed':
    case 'artifact_changed':
      appendDiagnostic(next, {
        id: event.eventId,
        streamKind: 'room',
        eventType: event.eventType,
        summary: 'Room metadata changed and is represented by the latest snapshot.',
        sequence: event.sequence,
        payload,
      });
      break;
    case 'snapshot_required':
      if (options.snapshotReplay) {
        appendDiagnostic(next, {
          id: event.eventId,
          streamKind: 'room',
          eventType: event.eventType,
          summary: 'A historical Room snapshot marker was replayed as an inert diagnostic.',
          sequence: event.sequence,
          payload,
        });
        break;
      }
      next.needsSnapshot = true;
      next.gap = {
        expectedSequence: state.lastSequence + 1,
        receivedSequence: event.sequence,
        receivedEventId: event.eventId,
      };
      return { state: next, disposition: 'snapshot-required' };
    case 'unknown':
      appendDiagnostic(next, {
        id: event.eventId,
        streamKind: 'room',
        eventType: event.rawEventType || 'unknown',
        summary: 'Unsupported room event was retained for diagnostics.',
        sequence: event.sequence,
        payload,
      });
      break;
  }
  return { state: next, disposition: 'applied' };
}

export function reduceRoomEvents(
  state: RoomProjectionState,
  events: readonly UiRoomEvent[],
): RoomProjectionState {
  let next = state;
  let index = 0;
  while (index < events.length) {
    const event = events[index];
    if (!event) break;
    if (!canStartRoomDeltaBatch(next, event)) {
      next = reduceRoomEvent(next, event).state;
      index += 1;
      continue;
    }

    let end = index + 1;
    let delta = text(event.payload.delta);
    let last = event;
    while (end < events.length) {
      const candidate = events[end];
      if (!candidate || !canMergeRoomDelta(last, candidate)) break;
      delta += text(candidate.payload.delta);
      last = candidate;
      end += 1;
    }

    const reduced = reduceRoomEvent(next, {
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

export function appendOptimisticRoomMessage(
  state: RoomProjectionState,
  input: OptimisticRoomMessageInput,
): RoomProjectionState {
  if (!input.clientMessageId.trim()) throw new TypeError('clientMessageId must not be empty');
  if (state.optimisticByClientMessageId[input.clientMessageId]) return state;
  const next = cloneState(state);
  const id = `local-room:${input.clientMessageId}`;
  const requestedAnswerToPostId = text(input.answerToPostId);
  const answeredPost = requestedAnswerToPostId
    ? state.messagesById[requestedAnswerToPostId]
    : undefined;
  const answerToPostId = answeredPost?.question
    ? requestedAnswerToPostId
    : '';
  const turnId = answerToPostId
    ? answeredPost!.turnId
    : `local-room-turn:${input.clientMessageId}`;
  const message: RoomMessageProjection = {
    id,
    roomId: state.roomId,
    turnId,
    participantId: null,
    sourceSessionId: '',
    role: 'user',
    status: 'queued',
    text: input.text,
    message: roomUserMessage({
      id,
      roomId: state.roomId,
      turnId,
      text: input.text,
      status: 'queued',
      attachments: input.attachments ?? [],
      createdAtMs: input.nowMs,
      clientMessageId: input.clientMessageId,
    }),
    clientMessageId: input.clientMessageId,
    projectionKind: 'optimistic',
    rootId: turnId,
    ...(answerToPostId ? { answerToPostId } : {}),
    ...(text(input.retryOfRootId) ? { retryOfRootId: text(input.retryOfRootId) } : {}),
    createdAtMs: input.nowMs,
  };
  next.messagesById[id] = message;
  next.messageOrder.push(id);
  next.optimisticByClientMessageId[input.clientMessageId] = id;
  attachMessage(next, message);
  if (!answerToPostId) next.turnsById[turnId].status = 'queued';
  return next;
}

export function roomActivityLaneIdentity(
  activity: RoomActivityProjection,
): RoomActivityLaneIdentity {
  const rootId = text(activity.payload.rootId) || activity.turnId;
  const participantId =
    text(activity.payload.targetParticipantId)
    || activity.participantId
    || activity.sourceSessionId
    || 'router';
  const dispatchId =
    text(activity.payload.dispatchId)
    || text(activity.payload.childDispatchId)
    || activity.sourceSessionId
    || text(activity.payload.sourceEventId)
    || activity.id;
  const sourceTurnId = text(activity.payload.sourceTurnId);
  const sourceLoopId = text(activity.payload.sourceLoopId);
  return {
    rootId,
    participantId,
    dispatchId,
    sourceTurnId,
    sourceLoopId,
    key: roomExecutionLaneKey(
      rootId,
      participantId,
      dispatchId,
      sourceLoopId || sourceTurnId,
    ),
  };
}

export function roomExecutionLaneKey(
  rootId: string,
  participantId: string,
  dispatchId: string,
  sourceLoopId = '',
): string {
  return [rootId, participantId, dispatchId, sourceLoopId]
    .filter((value, index) => index < 3 || Boolean(value))
    .join('\u001f');
}
export function selectRoomParticipantPublicProgress(
  state: RoomProjectionState,
): RoomParticipantPublicProgressProjection[] {
  const latestByIdentity = new Map<string, RoomParticipantPublicProgressProjection>();
  const retainLatest = (candidate: RoomParticipantPublicProgressProjection) => {
    const laneIdentity = candidate.participantId || candidate.sourceSessionId;
    if (!laneIdentity) return;
    const identity = [candidate.rootId, laneIdentity, candidate.kind].join('\u0000');
    const previous = latestByIdentity.get(identity);
    if (!previous || candidate.updatedAtMs >= previous.updatedAtMs) {
      latestByIdentity.set(identity, candidate);
    }
  };

  for (const activityId of state.activityOrder) {
    const activity = state.activitiesById[activityId];
    if (!activity) continue;
    if (!roomActivityHasPublicInformation(activity)) continue;
    const sourceEventType = text(activity.payload.sourceEventType);
    const activityKind = text(activity.payload.activityKind);
    retainLatest({
      participantId: activity.participantId ?? '',
      sourceSessionId: activity.sourceSessionId,
      rootId: text(activity.payload.rootId) || activity.turnId,
      dispatchId: text(activity.payload.dispatchId),
      kind: roomParticipantProgressKind(activity.kind, sourceEventType, activityKind),
      status: activity.status,
      summary: roomParticipantProgressSummary(activity.summary, sourceEventType, activity.kind),
      data: activity.payload,
      updatedAtMs: activity.updatedAtMs ?? activity.createdAtMs,
    });
  }

  for (const messageId of state.messageOrder) {
    const message = state.messagesById[messageId];
    if (
      !message
      || message.role !== 'assistant'
      || message.projectionKind !== 'post'
      || (!message.participantId && !message.sourceSessionId)
    ) continue;
    const summary = message.text.trim();
    if (!summary) continue;
    retainLatest({
      participantId: message.participantId ?? '',
      sourceSessionId: message.sourceSessionId,
      rootId: message.rootId || message.turnId,
      dispatchId: message.dispatchId ?? '',
      kind: 'post',
      status: message.status === 'failed'
        ? 'failed'
        : message.status === 'aborted'
          ? 'aborted'
          : ['queued', 'streaming'].includes(message.status)
            ? 'running'
            : 'completed',
      summary,
      updatedAtMs: message.completedAtMs ?? message.createdAtMs,
    });
  }

  return [...latestByIdentity.values()].sort((left, right) => (
    right.updatedAtMs - left.updatedAtMs
    || (left.participantId || left.sourceSessionId).localeCompare(
      right.participantId || right.sourceSessionId,
    )
  ));
}

export function roomActivityHasPublicInformation(
  activity: RoomActivityProjection,
): boolean {
  if (['failed', 'waiting', 'aborted'].includes(activity.status)) return true;
  const sourceEventType = text(activity.payload.sourceEventType);
  const activityKind = text(activity.payload.activityKind);
  const isProgress = ['current_progress', 'progress'].includes(sourceEventType)
    || activityKind === 'work';
  if (!isProgress) return true;
  const summary = activity.summary.trim().replace(/[\s。.!！]+$/gu, '');
  if (!summary || summary === sourceEventType || summary === activity.kind) return false;
  return !/^(?:公开|伙伴|协作|工作|任务)?(?:进度|状态)(?:已经|已)?(?:同步|更新|完成)$/u.test(summary);
}

export function applyRoomSnapshot(
  state: RoomProjectionState,
  snapshot: RoomSnapshot,
): RoomProjectionState {
  const next = createRoomProjection(state.roomId);
  if (state.moderatorParticipantId) {
    next.moderatorParticipantId = state.moderatorParticipantId;
  }
  next.lastSequence = Math.max(0, snapshot.lastSequence);
  next.lastEventId = snapshot.resumeToken;
  next.resumeToken = snapshot.resumeToken;
  const clientIds = new Set(snapshot.messages.map((message) => message.clientMessageId).filter(Boolean));
  for (const message of snapshot.messages) upsertMessage(next, message);
  preserveOptimisticMessages(state, next, clientIds);
  preserveLogicalTurnAliases(state, next);
  return next;
}

export function parseRoomEventSnapshot(value: unknown): RoomEventSnapshot {
  const snapshot = parseContract('agent-room-snapshot.v1', value);
  const events = snapshot.events.map((event) => parseRoomEvent(event));
  if (snapshot.room.lastEventSequence !== snapshot.lastSequence) {
    throw new TypeError('Room snapshot metadata cursor does not match lastSequence');
  }
  if (events.length === 0) {
    if (
      snapshot.firstSequence !== 0 ||
      snapshot.lastSequence !== 0 ||
      snapshot.resumeToken !== ''
    ) {
      throw new TypeError('Empty Room snapshot must use a zero cursor');
    }
  } else {
    const first = events[0];
    const last = events[events.length - 1];
    if (
      first.sequence !== snapshot.firstSequence ||
      last.sequence !== snapshot.lastSequence ||
      last.resumeToken !== snapshot.resumeToken
    ) {
      throw new TypeError('Room snapshot bounds do not match its retained events');
    }
    for (const [index, event] of events.entries()) {
      if (
        event.roomId !== snapshot.room.id ||
        event.sequence !== snapshot.firstSequence + index
      ) {
        throw new TypeError('Room snapshot events must be contiguous and belong to the room');
      }
    }
    if (snapshot.firstSequence > 1 && !snapshot.truncated) {
      throw new TypeError('Room snapshot must disclose a truncated retained prefix');
    }
  }
  return { ...snapshot, events };
}

export function parseRoomConversationSnapshot(value: unknown): RoomConversationSnapshot {
  const envelope = parseContract('agent-room-conversation-snapshot.v1', value);
  const roomProbe = parseContract('agent-room-snapshot.v1', {
    schemaVersion: 'rag-ime.agent-room-snapshot.v1',
    ok: true,
    room: envelope.room,
    events: [],
    firstSequence: 0,
    lastSequence: 0,
    resumeToken: '',
    truncated: false,
  });
  const room = roomProbe.room;
  const events = envelope.events.map((event) => parseRoomEvent(event));
  if (room.lastEventSequence !== envelope.cursorSequence) {
    throw new TypeError('Room conversation metadata cursor does not match cursorSequence');
  }
  const expectedResumeToken = envelope.cursorSequence
    ? `${room.id}:${envelope.cursorSequence}`
    : '';
  if (envelope.resumeToken !== expectedResumeToken) {
    throw new TypeError('Room conversation resume token does not match its cursor');
  }
  if (events.length === 0) {
    if (envelope.firstEventSequence !== 0) {
      throw new TypeError('Empty Room conversation snapshot must use a zero event bound');
    }
  } else if (events[0]?.sequence !== envelope.firstEventSequence) {
    throw new TypeError('Room conversation snapshot first event bound does not match');
  }
  let previousSequence = 0;
  for (const event of events) {
    if (
      event.roomId !== room.id
      || event.sequence <= previousSequence
      || event.sequence > envelope.cursorSequence
    ) {
      throw new TypeError('Room conversation events must be ordered and belong to the room cursor');
    }
    if (event.eventType === 'participant_activity') {
      throw new TypeError('Room conversation snapshot must defer participant activity');
    }
    previousSequence = event.sequence;
  }
  return { ...envelope, room, events };
}

export function parseRoomEventPage(value: unknown): RoomEventPage {
  const page = parseContract('agent-room-event-page.v1', value);
  const items = page.items.map((event) => parseRoomEvent(event));
  if (!items.length) {
    if (page.firstSequence !== 0 || page.lastSequence !== 0 || page.hasMore) {
      throw new TypeError('Empty Room history page must use zero bounds and no earlier page');
    }
  } else {
    if (
      items[0]?.sequence !== page.firstSequence
      || items.at(-1)?.sequence !== page.lastSequence
    ) {
      throw new TypeError('Room history page bounds do not match its events');
    }
    for (const [index, event] of items.entries()) {
      if (
        event.roomId !== page.roomId
        || event.sequence !== page.firstSequence + index
      ) {
        throw new TypeError('Room history page events must be contiguous and belong to the Room');
      }
    }
    if (
      page.hasMore !== (page.retainedFirstSequence < page.firstSequence)
      || page.nextBeforeSequence !== (page.hasMore ? page.firstSequence : 0)
    ) {
      throw new TypeError('Room history page cursor does not match retained bounds');
    }
  }
  if (page.retainedPrefixTruncated !== (page.retainedFirstSequence > 1)) {
    throw new TypeError('Room history page must disclose a truncated retained prefix');
  }
  return { ...page, items };
}

export function replayRoomEventSnapshot(
  state: RoomProjectionState,
  snapshot: RoomEventSnapshot,
): RoomProjectionState {
  if (state.roomId !== snapshot.room.id) {
    throw new TypeError('Room snapshot does not belong to the active Room');
  }
  let next = createRoomProjection(state.roomId);
  if (snapshot.room.moderatorParticipantId) {
    next.moderatorParticipantId = snapshot.room.moderatorParticipantId;
  }
  if (snapshot.firstSequence > 1) next.lastSequence = snapshot.firstSequence - 1;
  for (const event of snapshot.events) {
    const reduced = reduceRoomEvent(next, event, { snapshotReplay: true });
    if (reduced.disposition !== 'applied') {
      throw new TypeError(`Room snapshot replay failed: ${reduced.disposition}`);
    }
    next = reduced.state;
  }
  if (next.lastSequence !== snapshot.lastSequence) {
    throw new TypeError('Room snapshot replay did not reach its declared cursor');
  }
  next.lastEventId = snapshot.resumeToken;
  next.resumeToken = snapshot.resumeToken;
  const clientIds = new Set(
    Object.values(next.messagesById)
      .map((message) => message.clientMessageId)
      .filter((value): value is string => Boolean(value)),
  );
  preserveOptimisticMessages(state, next, clientIds);
  preserveLogicalTurnAliases(state, next);
  return next;
}

export function replayRoomConversationSnapshot(
  state: RoomProjectionState,
  snapshot: RoomConversationSnapshot,
): RoomProjectionState {
  if (state.roomId !== snapshot.room.id) {
    throw new TypeError('Room conversation snapshot does not belong to the active Room');
  }
  let next = createRoomProjection(state.roomId);
  if (snapshot.room.moderatorParticipantId) {
    next.moderatorParticipantId = snapshot.room.moderatorParticipantId;
  }
  for (const event of snapshot.events) {
    // Conversation snapshots intentionally omit heavyweight activity events.
    // Advance to the real event immediately before each retained event so the
    // normal reducer still validates ordering without mistaking deferral for
    // transport loss.
    next.lastSequence = event.sequence - 1;
    const reduced = reduceRoomEvent(next, event, { snapshotReplay: true });
    if (reduced.disposition !== 'applied') {
      throw new TypeError(`Room conversation snapshot replay failed: ${reduced.disposition}`);
    }
    next = reduced.state;
  }
  next.lastSequence = snapshot.cursorSequence;
  next.lastEventId = snapshot.resumeToken;
  next.resumeToken = snapshot.resumeToken;
  next.needsSnapshot = false;
  next.gap = undefined;
  const clientIds = new Set(
    Object.values(next.messagesById)
      .map((message) => message.clientMessageId)
      .filter((value): value is string => Boolean(value)),
  );
  preserveOptimisticMessages(state, next, clientIds);
  preserveLogicalTurnAliases(state, next);
  return next;
}

/**
 * Snapshot replay rebuilds turns from authoritative events, so it cannot see
 * the provisional turn that was replaced during the live stream. Carry the
 * alias only from this in-memory projection, keyed by the user's client
 * message id and explicit retry lineage; it is a UI continuity hint, never a
 * persisted Room identity.
 */
function preserveLogicalTurnAliases(
  previous: RoomProjectionState,
  next: RoomProjectionState,
): void {
  const aliasesByClientMessageId = new Map<string, string>();
  const aliasesByTurnId = new Map<string, string>();
  for (const turn of Object.values(previous.turnsById)) {
    const alias = turn.logicalRootId
      || (!turn.retryOfRootId && turn.id.startsWith('local-room-turn:') ? turn.id : '');
    if (!alias) continue;
    aliasesByTurnId.set(turn.id, alias);
    if (turn.rootId) aliasesByTurnId.set(turn.rootId, alias);
    for (const messageId of turn.messageIds) {
      const clientMessageId = previous.messagesById[messageId]?.clientMessageId;
      if (clientMessageId) aliasesByClientMessageId.set(clientMessageId, alias);
    }
  }
  /* A retry can itself be the only retained turn in the previous projection.
   * Resolve its explicit parent to the same alias without treating ordinary
   * new turns as descendants. */
  for (const turn of Object.values(previous.turnsById)) {
    if (aliasesByTurnId.has(turn.id)) continue;
    const alias = retryLineageAlias(previous, turn.retryOfRootId, aliasesByTurnId);
    if (!alias) continue;
    aliasesByTurnId.set(turn.id, alias);
    if (turn.rootId) aliasesByTurnId.set(turn.rootId, alias);
    for (const messageId of turn.messageIds) {
      const clientMessageId = previous.messagesById[messageId]?.clientMessageId;
      if (clientMessageId) aliasesByClientMessageId.set(clientMessageId, alias);
    }
  }
  if (!aliasesByClientMessageId.size && !aliasesByTurnId.size) return;
  for (const turn of Object.values(next.turnsById)) {
    const clientMessageId = turn.messageIds
      .map((messageId) => next.messagesById[messageId]?.clientMessageId)
      .find((value): value is string => Boolean(value));
    const alias = (clientMessageId ? aliasesByClientMessageId.get(clientMessageId) : undefined)
      || (turn.retryOfRootId ? aliasesByTurnId.get(turn.retryOfRootId) : undefined);
    if (!alias || alias === turn.id) continue;
    const writable = writableTurn(next, turn.id);
    if (writable) writable.logicalRootId = alias;
  }
}

function retryLineageAlias(
  projection: RoomProjectionState,
  retryOfRootId: string | undefined,
  aliasesByTurnId: ReadonlyMap<string, string>,
): string | undefined {
  let current = retryOfRootId || '';
  const visited = new Set<string>();
  while (current && !visited.has(current)) {
    visited.add(current);
    const alias = aliasesByTurnId.get(current);
    if (alias) return alias;
    current = projection.turnsById[current]?.retryOfRootId || '';
  }
  return undefined;
}

function preserveOptimisticMessages(
  state: RoomProjectionState,
  next: RoomProjectionState,
  serverClientIds: ReadonlySet<string | undefined>,
): void {
  for (const [clientMessageId, messageId] of Object.entries(
    state.optimisticByClientMessageId,
  )) {
    if (serverClientIds.has(clientMessageId)) continue;
    const message = state.messagesById[messageId];
    if (!message) continue;
    if (next.messagesById[messageId]) continue;
    next.messagesById[messageId] = message;
    next.messageOrder.push(messageId);
    next.optimisticByClientMessageId[clientMessageId] = messageId;
    attachMessage(next, message);
  }
}

export function abortRoomTurn(
  state: RoomProjectionState,
  turnId: string,
  nowMs: number,
): RoomProjectionState {
  if (!state.turnsById[turnId]) return state;
  const next = cloneState(state);
  completeTurn(next, turnId, 'aborted', nowMs);
  return next;
}

export function abortRoomParticipantTurn(
  state: RoomProjectionState,
  turnId: string,
  participantId: string,
  nowMs: number,
): RoomProjectionState {
  if (!state.turnsById[turnId] || !participantId) return state;
  const next = cloneState(state);
  completeParticipantTurn(
    next,
    {
      turnId,
      participantId,
    },
    '',
    'aborted',
    nowMs,
  );
  return next;
}

function applyUserMessage(
  state: RoomProjectionState,
  event: UiRoomEvent,
  payload: Record<string, unknown>,
): void {
  // A normal Room send is keyed by `clientMessageId`; an in-flight steer uses
  // the same causal contract under `clientActionId`. Treat both as the one
  // user-authored publication identity so a replay with a fresh event/message
  // id replaces its earlier projection instead of printing another bubble.
  const clientMessageId = text(payload.clientMessageId) || text(payload.clientActionId);
  const retryOfRootId = text(payload.retryOfRootId);
  const attachments = roomAttachmentReceipts(payload.attachmentReceipts, event.roomId);
  const rawAnswerText = text(payload.text ?? payload.message);
  const explicitAnswerToPostId = text(payload.answerToPostId);
  const pendingQuestion = state.pendingUserQuestion;
  const matchesPendingQuestion = Boolean(
    pendingQuestion
    && event.sequence > pendingQuestion.sequence
    && event.roomId === pendingQuestion.roomId
    && event.turnId === pendingQuestion.rootId
    && explicitAnswerToPostId === pendingQuestion.postId
  );
  const answerToPostId = matchesPendingQuestion
    ? pendingQuestion!.postId
    : explicitAnswerToPostId;
  const selectedOption = matchesPendingQuestion
    ? pendingQuestion!.options.find((option) => option.value === rawAnswerText)
    : undefined;
  const answerText = text(payload.displayText) || selectedOption?.label || rawAnswerText;
  const actorUserId = text(payload.actorUserId).trim().slice(0, 160);
  const actorDisplayName = text(payload.actorDisplayName).trim().slice(0, 120);
  if (matchesPendingQuestion) {
    updateQuestionMessage(state, pendingQuestion!.postId, 'answered', answerText);
    state.pendingUserQuestion = undefined;
  }
  const message: RoomMessageProjection = {
    id: text(payload.messageId) || `${event.eventId}:user`,
    roomId: event.roomId,
    turnId: event.turnId,
    participantId: event.participantId,
    sourceSessionId: event.sourceSessionId,
    sourceEventId: text(payload.sourceEventId) || event.eventId,
    sequence: event.sequence,
    role: 'user',
    status: 'completed',
    text: answerText,
    message: roomUserMessage({
      id: text(payload.messageId) || `${event.eventId}:user`,
      roomId: event.roomId,
      turnId: event.turnId,
      text: answerText,
      status: 'completed',
      attachments,
      createdAtMs: event.createdAtMs,
      clientMessageId,
    }),
    projectionKind: 'post',
    rootId: text(payload.rootId) || event.turnId,
    ...(clientMessageId ? { clientMessageId } : {}),
    ...(actorUserId ? { actorUserId } : {}),
    ...(actorDisplayName ? { actorDisplayName } : {}),
    ...(answerToPostId ? { answerToPostId } : {}),
    ...(retryOfRootId ? { retryOfRootId } : {}),
    createdAtMs: event.createdAtMs,
    completedAtMs: event.createdAtMs,
  };
  upsertMessage(state, message, clientMessageId);
}

function roomAttachmentReceipts(value: unknown, roomId: string): RoomAttachmentReceipt[] {
  if (!Array.isArray(value)) return [];
  const result: RoomAttachmentReceipt[] = [];
  for (const item of value.slice(0, 8)) {
    if (Object.keys(record(item)).length === 0) continue;
    const mediaId = text(item.mediaId);
    const ownerRoomId = text(item.roomId);
    const mimeType = text(item.mimeType);
    const sha256 = text(item.sha256);
    const byteSize = Number(item.byteSize);
    if (
      item.ownerType !== 'room'
      || ownerRoomId !== roomId
      || !/^media_[A-Za-z0-9_-]{12,80}$/u.test(mediaId)
      || !isComposerAttachmentMimeType(mimeType)
      || !Number.isInteger(byteSize)
      || byteSize < 1
      || byteSize > MAX_COMPOSER_ATTACHMENT_BYTES
      || !/^[0-9a-f]{64}$/u.test(sha256)
    ) continue;
    result.push({
      mediaId,
      roomId,
      fileName: text(item.fileName).slice(0, 160) || '附件',
      mimeType: mimeType.toLowerCase(),
      byteSize,
      sha256,
      width: finiteDimension(item.width),
      height: finiteDimension(item.height),
    });
  }
  return result;
}

function roomUserMessage({
  id,
  roomId,
  turnId,
  text: content,
  status,
  attachments,
  createdAtMs,
  clientMessageId,
}: {
  id: string;
  roomId: string;
  turnId: string;
  text: string;
  status: 'queued' | 'completed';
  attachments: RoomAttachmentReceipt[];
  createdAtMs: number;
  clientMessageId?: string;
}): UiAgentMessage {
  return {
    schemaVersion: 'rag-ime.agent-message.v1',
    id,
    sessionId: `room:${roomId}`,
    turnId,
    role: 'user',
    status,
    blocks: [
      {
        id: `${id}:text`,
        type: 'text',
        status,
        presentationKind: 'markdown',
        data: { text: content },
      },
      // Images stay inline thumbnails; every other file renders through the
      // managed file card so a pasted PDF never shows a broken image frame.
      ...attachments.map((attachment) => ({
        id: `${id}:attachment:${attachment.mediaId}`,
        type: isComposerImageMimeType(attachment.mimeType) ? ('image' as const) : ('file' as const),
        status,
        presentationKind: isComposerImageMimeType(attachment.mimeType) ? 'managed_image' : 'managed_file',
        data: {
          mediaId: attachment.mediaId,
          fileName: attachment.fileName,
          mimeType: attachment.mimeType,
          byteSize: attachment.byteSize,
          sha256: attachment.sha256,
          receiptUrl: `/api/agent/media/${encodeURIComponent(attachment.mediaId)}/content?roomId=${encodeURIComponent(roomId)}`,
          width: attachment.width,
          height: attachment.height,
          alt: attachment.fileName,
        },
      })),
    ],
    attachments: attachments.map((attachment) => attachment.mediaId),
    citations: [],
    createdAtMs,
    ...(status === 'completed' ? { completedAtMs: createdAtMs } : {}),
    ...(clientMessageId ? { clientMessageId } : {}),
  };
}

function finiteDimension(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 && parsed <= 65_535 ? parsed : null;
}

function applyParticipantDelta(
  state: RoomProjectionState,
  event: UiRoomEvent,
  payload: Record<string, unknown>,
): void {
  const rootId = text(payload.rootId) || event.turnId;
  const dispatchId = text(payload.dispatchId);
  const sourceTurnId = text(payload.sourceTurnId);
  const sourceLoopId = text(payload.sourceLoopId);
  const sourceMessageId = text(payload.messageId);
  const sourceBlockId = text(payload.blockId);
  const id = executionMessageId(event, payload);
  const replacesProvisional = payload.replaceBlock === true || payload.replaceContent === true;
  const existing = state.messagesById[id];
  const message: RoomMessageProjection = existing
    ? {
        ...existing,
        status: 'streaming',
        text: replacesProvisional ? text(payload.delta) : existing.text + text(payload.delta),
      }
    : {
        id,
        roomId: event.roomId,
        turnId: event.turnId,
        participantId: event.participantId,
        sourceSessionId: event.sourceSessionId,
        sourceEventId: text(payload.sourceEventId) || event.eventId,
        sequence: event.sequence,
        role: 'assistant',
        status: 'streaming',
        text: text(payload.delta),
        projectionKind: 'execution',
        rootId,
        ...(dispatchId ? { dispatchId } : {}),
        ...(sourceTurnId ? { sourceTurnId } : {}),
        ...(sourceLoopId ? { sourceLoopId } : {}),
        ...(sourceMessageId ? { sourceMessageId } : {}),
        ...(sourceBlockId ? { sourceBlockId } : {}),
        createdAtMs: event.createdAtMs,
      };
  upsertMessage(state, message);
}

function applyParticipantMessage(
  state: RoomProjectionState,
  event: UiRoomEvent,
  payload: Record<string, unknown>,
): void {
  const parsed = tryParseAgentMessage(payload.message);
  if (!parsed.ok) {
    appendDiagnostic(state, {
      id: `${event.eventId}:invalid-message`,
      streamKind: 'room',
      eventType: 'participant_message_invalid',
      summary: 'A malformed participant message was skipped.',
      sequence: event.sequence,
      payload,
    });
    return;
  }
  const clientMessageId = text(payload.clientMessageId) || parsed.value.clientMessageId || '';
  const sourceLoopId = text(payload.sourceLoopId);
  const message: RoomMessageProjection = {
    id: sourceLoopId
      ? `${parsed.value.id}\u001f${sourceLoopId}`
      : parsed.value.id,
    roomId: event.roomId,
    turnId: event.turnId || parsed.value.turnId,
    participantId: event.participantId,
    sourceSessionId: event.sourceSessionId,
    sourceEventId: text(payload.sourceEventId) || event.eventId,
    ...(text(payload.sourceTurnId) ? {
      sourceTurnId: text(payload.sourceTurnId),
    } : {}),
    ...(sourceLoopId ? {
      sourceLoopId,
    } : {}),
    sequence: event.sequence,
    role: parsed.value.role === 'user' ? 'user' : 'assistant',
    status: parsed.value.status,
    text: messageText(parsed.value),
    message: parsed.value,
    projectionKind: 'post',
    rootId: text(payload.rootId) || event.turnId,
    ...(text(payload.dispatchId) ? { dispatchId: text(payload.dispatchId) } : {}),
    ...(clientMessageId ? { clientMessageId } : {}),
    createdAtMs: parsed.value.createdAtMs,
    ...(parsed.value.completedAtMs == null
      ? {}
      : { completedAtMs: parsed.value.completedAtMs }),
  };
  const provisional = findProvisionalMessage(state, {
    rootId: message.rootId || message.turnId,
    dispatchId: message.dispatchId,
    sourceLoopId: message.sourceLoopId,
    participantId: message.participantId,
    sourceSessionId: message.sourceSessionId,
    messageIds: [message.id, text(payload.messageId)],
    blockIds: [
      text(payload.blockId),
      ...parsed.value.blocks.map((block) => block.id),
    ],
  });
  if (provisional && provisional.id !== message.id) {
    replaceProvisionalMessage(state, provisional.id, message);
    return;
  }
  if (findEquivalentCanonicalRoomPost(state, message)) return;
  upsertMessage(state, message, clientMessageId);
}

function applyRoomPost(
  state: RoomProjectionState,
  event: UiRoomEvent,
  payload: Record<string, unknown>,
): void {
  let post: RoomPostV2;
  try {
    post = parseContract('room-post.v2', payload.post);
  } catch {
    appendDiagnostic(state, {
      id: `${event.eventId}:invalid-room-post`,
      streamKind: 'room',
      eventType: 'room_post_invalid',
      summary: 'A malformed RoomPost was skipped.',
      sequence: event.sequence,
      payload,
    });
    return;
  }
  const postSequence = post.chronology?.roomEventSequence ?? event.sequence;
  const postCreatedAtMs = post.chronology?.createdAtMs ?? post.createdAtMs;
  const pendingAnswerToPostId = (
    state.pendingUserQuestion
    && postSequence > state.pendingUserQuestion.sequence
    && post.roomId === state.pendingUserQuestion.roomId
    && post.rootId === state.pendingUserQuestion.rootId
    && post.publicationSource.kind === 'user'
    && post.authorActorRef.startsWith('user:')
  ) ? state.pendingUserQuestion.postId : '';
  const pendingAnswerQuestion = pendingAnswerToPostId
    ? state.messagesById[pendingAnswerToPostId]?.question
    : undefined;
  const publicPostContent = pendingAnswerQuestion?.options.find(
    (option) => option.value === post.content,
  )?.label || post.content;
  updatePendingUserQuestion(state, event, post);
  const fallbackBlock: UiAgentMessage['blocks'][number] = {
    schemaVersion: 'rag-ime.agent-block.v1',
    id: `${post.postId}:text`,
    type: 'text',
    status: 'completed',
    presentationKind: 'markdown',
    data: { text: publicPostContent },
    source: { kind: post.publicationSource.kind, ref: post.publicationSource.ref },
    visibility: post.visibility === 'room' ? 'room_post' : 'root_post',
    generation: post.generation,
  };
  const clientMessageId = post.publicationSource.kind === 'user'
    ? post.publicationSource.ref
    : '';
  const priorClientMessageId = clientMessageId
    ? state.messageOrder.find((messageId) => (
        state.messagesById[messageId]?.clientMessageId === clientMessageId
      ))
    : undefined;
  const answerToPostId = pendingAnswerToPostId
    || (priorClientMessageId
      ? state.messagesById[priorClientMessageId]?.answerToPostId
      : undefined)
    || '';
  const message: RoomMessageProjection = {
    id: post.postId,
    roomId: event.roomId,
    turnId: post.rootId,
    participantId: event.participantId,
    sourceSessionId: event.sourceSessionId,
    sourceEventId: post.chronology?.roomEventId
      || text(payload.sourceEventId)
      || post.publicationSource.ref
      || event.eventId,
    ...(text(payload.sourceTurnId) ? {
      sourceTurnId: text(payload.sourceTurnId),
    } : {}),
    ...(text(payload.sourceLoopId) ? {
      sourceLoopId: text(payload.sourceLoopId),
    } : {}),
    sequence: postSequence,
    ...(post.chronology ? { chronology: { ...post.chronology } } : {}),
    role: post.publicationSource.kind === 'user' ? 'user' : 'assistant',
    status: 'completed',
    text: publicPostContent,
    projectionKind: 'post',
    postKind: post.kind,
    rootId: post.rootId,
    ...(state.pendingUserQuestion?.postId === post.postId ? {
      question: {
        prompt: state.pendingUserQuestion.prompt,
        options: [...state.pendingUserQuestion.options],
        status: 'pending',
      },
    } satisfies Pick<RoomMessageProjection, 'question'> : {}),
    ...(clientMessageId ? { clientMessageId } : {}),
    ...(answerToPostId ? { answerToPostId } : {}),
    ...(post.dispatchId ? { dispatchId: post.dispatchId } : {}),
    ...(post.mentions?.length ? { mentionedParticipantIds: [...post.mentions] } : {}),
    message: {
      schemaVersion: 'rag-ime.agent-message.v1',
      id: post.postId,
      sessionId: event.sourceSessionId || `room:${event.roomId}`,
      turnId: post.rootId,
      role: post.publicationSource.kind === 'user' ? 'user' : 'assistant',
      status: 'completed',
      blocks: (post.blocks?.length ? post.blocks : [fallbackBlock]) as UiAgentMessage['blocks'],
      attachments: [],
      citations: [],
      createdAtMs: postCreatedAtMs,
      completedAtMs: postCreatedAtMs,
    },
    createdAtMs: postCreatedAtMs,
    completedAtMs: postCreatedAtMs,
  };
  const provisional = findProvisionalMessage(state, {
    rootId: post.rootId,
    dispatchId: post.dispatchId,
    sourceLoopId: message.sourceLoopId,
    participantId: event.participantId,
    sourceSessionId: event.sourceSessionId,
    messageIds: [post.postId, post.publicationSource.ref],
    blockIds: (post.blocks ?? []).flatMap((block) => [block.id, block.ref]),
  });
  const superseded = provisional
    ?? findEquivalentRuntimeReply(state, message);
  if (superseded) {
    replaceProvisionalMessage(state, superseded.id, message);
  } else {
    upsertMessage(state, message, clientMessageId);
  }
  markPublishedDispatchTerminal(state, event, post);
  if (
    (post.kind === 'work_result' || post.kind === 'result')
    && hasFormalWorkResultEvidence(state, post.rootId)
  ) {
    const turn = state.turnsById[post.rootId];
    if (turn) settleRootWhenAllDispatchesTerminal(state, turn, event.createdAtMs);
  }
}


function updatePendingUserQuestion(
  state: RoomProjectionState,
  event: UiRoomEvent,
  post: RoomPostV2,
): void {
  const pending = state.pendingUserQuestion;
  const postSequence = post.chronology?.roomEventSequence ?? event.sequence;
  if (
    pending
    && postSequence > pending.sequence
    && post.roomId === pending.roomId
    && post.rootId === pending.rootId
    && post.roomId === event.roomId
    && post.rootId === event.turnId
    && post.publicationSource.kind === 'user'
    && post.authorActorRef.startsWith('user:')
  ) {
    const selectedOption = pending.options.find(
      (option) => option.value === post.content,
    );
    updateQuestionMessage(
      state,
      pending.postId,
      'answered',
      selectedOption?.label || post.content,
    );
    state.pendingUserQuestion = undefined;
    return;
  }

  if (
    post.kind !== 'wait'
    || post.visibility !== 'room'
    || post.publicationSource.kind !== 'room_commit'
    || !post.question
    || post.roomId !== event.roomId
    || post.rootId !== event.turnId
    || (pending && postSequence <= pending.sequence)
  ) return;

  const options = [...post.question.options];
  if (
    (options.length !== 0 && (options.length < 2 || options.length > 5))
    || new Set(options.map((option) => option.value)).size !== options.length
    || options.filter((option) => option.recommended).length > 1
  ) return;

  if (pending && pending.postId !== post.postId) {
    updateQuestionMessage(state, pending.postId, 'superseded');
  }
  state.pendingUserQuestion = {
    postId: post.postId,
    roomId: post.roomId,
    rootId: post.rootId,
    sequence: postSequence,
    prompt: post.question.prompt,
    options,
  };
}

function updateQuestionMessage(
  state: RoomProjectionState,
  postId: string,
  status: RoomMessageQuestionProjection['status'],
  answer?: string,
): void {
  const message = state.messagesById[postId];
  if (!message?.question) return;
  state.messagesById[postId] = {
    ...message,
    question: {
      ...message.question,
      status,
      ...(answer ? { answer } : {}),
    },
  };
}
function markPublishedDispatchTerminal(
  state: RoomProjectionState,
  event: UiRoomEvent,
  post: RoomPostV2,
): void {
  if (post.publicationSource.kind !== 'room_commit' || !post.dispatchId) return;

  const participantId = event.participantId ?? '';
  const turn = ensureTurn(state, post.rootId, post.createdAtMs);
  turn.rootId = post.rootId;
  turn.dispatchIds ??= [];
  turn.terminalDispatchIds ??= [];
  turn.dispatchParticipantIds ??= {};
  if (!turn.dispatchIds.includes(post.dispatchId)) {
    turn.dispatchIds.push(post.dispatchId);
  }
  if (participantId) {
    turn.dispatchParticipantIds[post.dispatchId] = participantId;
    if (!turn.participantIds.includes(participantId)) {
      turn.participantIds.push(participantId);
    }
  }
  if (!turn.terminalDispatchIds.includes(post.dispatchId)) {
    turn.terminalDispatchIds.push(post.dispatchId);
  }

  if (participantId) {
    const participantDispatches = turn.dispatchIds.filter(
      (dispatchId) => turn.dispatchParticipantIds?.[dispatchId] === participantId,
    );
    if (
      participantDispatches.length > 0
      && participantDispatches.every(
        (dispatchId) => turn.terminalDispatchIds?.includes(dispatchId),
      )
    ) {
      turn.terminalParticipantIds ??= [];
      if (!turn.terminalParticipantIds.includes(participantId)) {
        turn.terminalParticipantIds.push(participantId);
      }
    }
  }
  // Publishing settles one historical room_commit lane. Pi-composed Session
  // terminals settle the Root only after every known Dispatch is terminal;
  // publication alone never unlocks the whole turn.
  turn.updatedAtMs = Math.max(turn.updatedAtMs, post.createdAtMs);
}

interface ProvisionalMessageIdentity {
  rootId: string;
  dispatchId?: string;
  sourceLoopId?: string;
  participantId: string | null;
  sourceSessionId: string;
  messageIds: string[];
  blockIds: string[];
}

function findProvisionalMessage(
  state: RoomProjectionState,
  identity: ProvisionalMessageIdentity,
): RoomMessageProjection | undefined {
  const messageIds = new Set(identity.messageIds.filter(Boolean));
  const blockIds = new Set(identity.blockIds.filter(Boolean));
  const candidates = Object.values(state.messagesById)
    .filter((candidate) => (
      candidate.projectionKind === 'execution'
      && candidate.status === 'streaming'
      && candidate.rootId === identity.rootId
      && (
        candidate.participantId === identity.participantId
        || Boolean(
          candidate.sourceSessionId
          && candidate.sourceSessionId === identity.sourceSessionId,
        )
      )
      && (!identity.dispatchId || candidate.dispatchId === identity.dispatchId)
      && (!identity.sourceLoopId || candidate.sourceLoopId === identity.sourceLoopId)
    ))
    .sort((left, right) => right.createdAtMs - left.createdAtMs);
  const aliased = candidates.find((candidate) => (
    Boolean(candidate.sourceMessageId && messageIds.has(candidate.sourceMessageId))
    || Boolean(candidate.sourceBlockId && blockIds.has(candidate.sourceBlockId))
  ));
  return aliased ?? (candidates.length === 1 ? candidates[0] : undefined);
}

function findEquivalentCanonicalRoomPost(
  state: RoomProjectionState,
  message: RoomMessageProjection,
): RoomMessageProjection | undefined {
  return findEquivalentAssistantReply(state, message, true);
}

function findEquivalentRuntimeReply(
  state: RoomProjectionState,
  message: RoomMessageProjection,
): RoomMessageProjection | undefined {
  return findEquivalentAssistantReply(state, message, false);
}

function findEquivalentAssistantReply(
  state: RoomProjectionState,
  message: RoomMessageProjection,
  canonical: boolean,
): RoomMessageProjection | undefined {
  const sourceAliases = roomMessageSourceAliases(message);
  if (
    message.role !== 'assistant'
    || sourceAliases.size === 0
  ) return undefined;
  return Object.values(state.messagesById)
    .filter((candidate) => (
      candidate.id !== message.id
      && candidate.role === 'assistant'
      && candidate.projectionKind === 'post'
      && Boolean(candidate.postKind) === canonical
      && candidate.rootId === message.rootId
      && candidate.dispatchId === message.dispatchId
      && (
        candidate.participantId === message.participantId
        || Boolean(
          candidate.sourceSessionId
          && candidate.sourceSessionId === message.sourceSessionId
        )
      )
      && roomMessagesShareSourceAlias(candidate, sourceAliases)
    ))
    .sort((left, right) => (
      (right.sequence ?? -1) - (left.sequence ?? -1)
      || right.createdAtMs - left.createdAtMs
      || right.id.localeCompare(left.id)
    ))[0];
}

function roomMessagesShareSourceAlias(
  candidate: RoomMessageProjection,
  sourceAliases: ReadonlySet<string>,
): boolean {
  for (const alias of roomMessageSourceAliases(candidate)) {
    if (sourceAliases.has(alias)) return true;
  }
  return false;
}

function roomMessageSourceAliases(message: RoomMessageProjection): Set<string> {
  return new Set([
    message.sourceEventId,
    message.sourceMessageId,
    message.sourceBlockId,
    message.message?.id,
  ].filter((value): value is string => Boolean(value)));
}

function replaceProvisionalMessage(
  state: RoomProjectionState,
  provisionalId: string,
  message: RoomMessageProjection,
): void {
  const provisional = state.messagesById[provisionalId];
  if (!provisional) {
    upsertMessage(state, message);
    return;
  }

  const orderIndex = state.messageOrder.indexOf(provisionalId);
  const finalAlreadyProjected = Boolean(state.messagesById[message.id]);
  if (orderIndex >= 0) {
    if (finalAlreadyProjected) {
      state.messageOrder.splice(orderIndex, 1);
    } else {
      state.messageOrder[orderIndex] = message.id;
    }
  }
  delete state.messagesById[provisionalId];
  state.messagesById[message.id] = message;

  const turn = writableTurn(state, provisional.turnId);
  if (turn) {
    turn.messageIds = turn.messageIds.filter(
      (id) => id !== provisionalId && id !== message.id,
    );
    turn.messageIds.push(message.id);
    turn.updatedAtMs = Math.max(
      turn.updatedAtMs,
      message.completedAtMs ?? message.createdAtMs,
    );
  } else {
    attachMessage(state, message);
  }
}

function upsertMessage(
  state: RoomProjectionState,
  message: RoomMessageProjection,
  clientMessageId = message.clientMessageId ?? '',
): void {
  const optimisticId = clientMessageId
    ? state.optimisticByClientMessageId[clientMessageId]
    : undefined;
  const acceptedId = clientMessageId
    ? state.messageOrder.find((messageId) => (
        messageId !== message.id
        && state.messagesById[messageId]?.clientMessageId === clientMessageId
      ))
    : undefined;
  const replacedId = optimisticId ?? acceptedId;
  let replacedExisting = false;
  let replacedTurnId = '';
  let replacedLogicalRootId = '';
  if (replacedId && replacedId !== message.id) {
    const index = state.messageOrder.indexOf(replacedId);
    const previous = state.messagesById[replacedId];
    replacedTurnId = previous?.turnId ?? '';
    replacedLogicalRootId = (replacedTurnId ? state.turnsById[replacedTurnId]?.logicalRootId : '')
      || (previous
      ? previous.rootId && previous.rootId !== previous.turnId
        ? previous.rootId
        : ''
      : '');
    if (!replacedLogicalRootId && replacedTurnId.startsWith('local-room-turn:')) {
      replacedLogicalRootId = replacedTurnId;
    }
    delete state.messagesById[replacedId];
    if (index >= 0) {
      state.messageOrder[index] = message.id;
      replacedExisting = true;
    }
    if (previous) detachMessage(state, previous);
  }
  if (clientMessageId) delete state.optimisticByClientMessageId[clientMessageId];
  if (!state.messagesById[message.id] && !replacedExisting) state.messageOrder.push(message.id);
  state.messagesById[message.id] = message;
  attachMessage(state, message);
  if (replacedLogicalRootId) {
    const turn = writableTurn(state, message.turnId);
    if (turn) turn.logicalRootId = replacedLogicalRootId;
  }
}

function upsertActivity(
  state: RoomProjectionState,
  event: UiRoomEvent,
  payload: Record<string, unknown>,
  forcedStatus?: RoomActivityProjection['status'],
): void {
  const participantStatus = text(payload.status);
  const sourceEventType = text(payload.sourceEventType);
  const resolutionState = text(payload.resolutionState || payload.state);
  const requestKind = text(payload.requestKind);
  const isCompletedRoomLifecycle =
    event.eventType === 'participant_status' &&
    ['room_created', 'room_archived', 'room_restored'].includes(
      participantStatus,
    );
  const participantId = text(payload.participantId) || event.participantId;
  const sourceSessionId = text(payload.sourceSessionId) || event.sourceSessionId;
  const id = roomActivityId(event, payload);
  const existing = state.activitiesById[id];
  const approvalId = text(payload.approvalId);
  const unresolved = !['approved', 'rejected', 'applied', 'resolved', 'cancelled'].includes(
    resolutionState,
  );
  const automatedApproval = Boolean(approvalId)
    && !approvalNeedsHumanDecision(payload)
    && unresolved;
  const pendingInteraction = (
    (Boolean(approvalId) && approvalNeedsHumanDecision(payload))
    || ['memory_review', 'plan_review', 'user_input_required', 'grouped_questions'].includes(requestKind)
    || sourceEventType === 'user_input_required'
    || (text(payload.method) === 'select' && Array.isArray(payload.options))
  ) && unresolved;
  const automaticPolicyAuthorizationEvent = Boolean(
    approvalId
    && payload.automatic === true
    && text(payload.decisionMode || payload.mode) === 'policy'
    && ['approval_required', 'approval_resolved'].includes(sourceEventType)
  );
  const automaticPolicyAuthorizationReceipt = Boolean(
    automaticPolicyAuthorizationEvent
    && existing
    && text(existing.payload.sourceEventType).startsWith('tool_')
  );
  const automaticPolicyAuthorizationFailed = automaticPolicyAuthorizationEvent && (
    payload.isError === true
    || ['deny', 'denied', 'rejected', 'expired', 'stale', 'failed'].includes(
      text(payload.resolutionState || payload.state || payload.decision),
    )
  );
  const candidateStatus = forcedStatus ?? (
    automaticPolicyAuthorizationFailed
      ? 'failed'
      : automaticPolicyAuthorizationReceipt
      ? automaticPolicyAuthorizationFailed ? 'failed' : existing!.status
      : payload.isError === true || participantStatus === 'failed'
      ? 'failed'
      : participantStatus === 'retry_wait' || pendingInteraction
        ? 'waiting'
      : automatedApproval
        ? 'running'
        : sourceEventType === 'tool_started'
          || sourceEventType === 'tool_progress'
          || (
            ['reasoning_summary', 'current_progress'].includes(sourceEventType)
            && !['completed', 'failed', 'aborted'].includes(text(payload.state))
          )
          ? 'running'
          : event.eventType === 'participant_status' && !isCompletedRoomLifecycle
            ? 'running'
            : 'completed'
  );
  const staleToolStreamingUpdate = Boolean(
    existing
    && ['completed', 'failed', 'aborted'].includes(existing.status)
    && (
      ['tool_started', 'tool_progress'].includes(sourceEventType)
      || (
        sourceEventType === 'tool_finished'
        && existing.status === 'failed'
        && ['deny', 'denied', 'rejected', 'expired', 'stale', 'failed'].includes(
          text(existing.payload.approvalResolutionState),
        )
        && payload.isError !== true
      )
    ),
  );
  const status = staleToolStreamingUpdate ? existing!.status : candidateStatus;
  const activityPayload = staleToolStreamingUpdate
    ? existing!.payload
    : automaticPolicyAuthorizationReceipt || automaticPolicyAuthorizationFailed
      ? mergeAutomaticPolicyAuthorizationReceipt(
          existing,
          payload,
          automaticPolicyAuthorizationFailed,
        )
    : mergeRoomActivityPayload(
        existing,
        event,
        payload,
        status,
      );
  const activity: RoomActivityProjection = {
    id,
    sequence: existing?.sequence ?? event.sequence,
    turnId: event.turnId,
    participantId,
    sourceSessionId,
    kind: event.eventType,
    status,
    summary: staleToolStreamingUpdate
      || automaticPolicyAuthorizationReceipt
      || automaticPolicyAuthorizationFailed
      ? automaticPolicyAuthorizationFailed
        ? text(payload.summary ?? payload.error) || existing?.summary || text(payload.toolName)
        : existing?.summary || text(payload.toolName)
      : text(payload.summary ?? payload.message ?? payload.label ?? payload.toolName)
        || sourceEventType
        || event.eventType,
    payload: activityPayload,
    createdAtMs: existing?.createdAtMs ?? event.createdAtMs,
    updatedAtMs: staleToolStreamingUpdate ? existing!.updatedAtMs : event.createdAtMs,
  };
  if (!existing) state.activityOrder.push(id);
  state.activitiesById[id] = activity;
  const turn = ensureTurn(state, event.turnId, event.createdAtMs);
  turn.rootId = text(payload.rootId) || turn.rootId || event.turnId;
  const dispatchId = text(payload.dispatchId);
  const introducesDispatch = event.eventType === 'route_decision'
    || (
      event.eventType === 'participant_activity'
      && text(payload.activityKind) !== 'work'
    );
  if (dispatchId && introducesDispatch) {
    turn.dispatchIds ??= [];
    turn.dispatchParticipantIds ??= {};
    if (!turn.dispatchIds.includes(dispatchId)) turn.dispatchIds.push(dispatchId);
    turn.dispatchParticipantIds[dispatchId] = participantId ?? '';
  }
  if (!turn.activityIds.includes(id)) turn.activityIds.push(id);
  if (participantId && !turn.participantIds.includes(participantId)) {
    turn.participantIds.push(participantId);
  }
  const childTerminalStatus = text(payload.phase || payload.status);
  if (
    text(payload.activityKind) === 'child'
    && dispatchId
    && ['completed', 'failed', 'aborted'].includes(childTerminalStatus)
  ) {
    completeParticipantTurn(
      state,
      event,
      dispatchId,
      childTerminalStatus as Extract<
        RoomTurnProjection['status'],
        'completed' | 'failed' | 'aborted'
      >,
      event.createdAtMs,
      text(payload.error),
    );
  }
  if (isCompletedRoomLifecycle) {
    completeTurn(state, event.turnId, 'completed', event.createdAtMs);
  }
}

function mergeAutomaticPolicyAuthorizationReceipt(
  previous: RoomActivityProjection | undefined,
  payload: Record<string, unknown>,
  failed: boolean,
): Record<string, unknown> {
  const previousPayload = previous?.payload ?? {};
  const merged: Record<string, unknown> = {
    ...previousPayload,
    ...payload,
    approvalSourceEventType: text(payload.sourceEventType),
    approvalResolutionState: text(payload.resolutionState || payload.state),
    sourceEventType: failed ? 'tool_finished' : previousPayload.sourceEventType,
  };
  // Authorization is an audit receipt inside an already-running Tool
  // lifecycle. It must not replace the Tool's public identity, progress, or
  // terminal state while both events intentionally share one toolCallId.
  for (const field of ['toolName', 'arguments', 'args'] as const) {
    if (previousPayload[field] !== undefined) merged[field] = previousPayload[field];
  }
  for (const field of ['summary', 'message', 'label', 'status', 'state'] as const) {
    if (failed) continue;
    if (previousPayload[field] !== undefined) merged[field] = previousPayload[field];
    else delete merged[field];
  }
  if (failed) merged.state = 'failed';
  return merged;
}

function roomActivityId(
  event: UiRoomEvent,
  payload: Record<string, unknown>,
): string {
  if (
    event.eventType === 'participant_status'
    && ['room_created', 'room_archived', 'room_restored'].includes(text(payload.status))
  ) return `${event.eventId}:activity`;
  const participantId = text(payload.participantId) || event.participantId;
  const rootId = text(payload.rootId) || event.turnId;
  const sourceSessionId = text(payload.sourceSessionId) || event.sourceSessionId;
  const executionScope =
    text(payload.dispatchId)
    || sourceSessionId
    || participantId
    || 'room';
  const lifecycleId = text(
    payload.toolCallId
    ?? payload.approvalId
    ?? payload.requestId
    ?? payload.runId,
  );
  if (lifecycleId) {
    return `${rootId}:${participantId ?? 'participant'}:${executionScope}:${lifecycleId}`;
  }
  const sourceKind = text(payload.sourceEventType || payload.activityKind);
  if (
    event.eventType === 'participant_status'
    || event.eventType === 'route_decision'
    || ['reasoning_summary', 'status_changed', 'current_progress', 'progress'].includes(
      sourceKind,
    )
  ) {
    return `${event.turnId}:${participantId ?? 'participant'}:${executionScope}:${sourceKind || event.eventType}`;
  }
  return `${event.eventId}:activity`;
}

function mergeRoomActivityPayload(
  previous: RoomActivityProjection | undefined,
  event: UiRoomEvent,
  payload: Record<string, unknown>,
  status: RoomActivityProjection['status'],
): Record<string, unknown> {
  const previousPayload = previous?.payload ?? {};
  const sourceEventType = text(payload.sourceEventType);
  const recordsPublicHistory = [
    'tool_started',
    'tool_progress',
    'tool_finished',
    'reasoning_summary',
    'current_progress',
    'progress',
    'status_changed',
  ].includes(sourceEventType);
  if (!recordsPublicHistory) {
    return { ...previousPayload, ...payload };
  }
  const suppliedHistory = Array.isArray(payload.progressHistory)
    ? payload.progressHistory
    : Array.isArray(previousPayload.progressHistory)
      ? previousPayload.progressHistory
      : [];
  const boundedHistory = suppliedHistory.slice(-20);
  const sourceEventId = text(payload.sourceEventId) || event.eventId;
  const history = boundedHistory.some((entry) => (
    text(record(entry).eventId ?? record(entry).sourceEventId) === sourceEventId
  ))
    ? boundedHistory
    : [...boundedHistory, {
        eventId: sourceEventId,
        kind: sourceEventType,
        status,
        summary: text(payload.summary ?? payload.message ?? payload.label),
        createdAtMs: event.createdAtMs,
      }].slice(-20);
  const merged: Record<string, unknown> = {
    ...previousPayload,
    ...payload,
    progressHistory: history,
  };
  const isToolLifecycle = ['tool_started', 'tool_progress', 'tool_finished'].includes(
    sourceEventType,
  );
  if (
    isToolLifecycle
    &&
    Object.keys(record(payload.arguments ?? payload.args)).length === 0
    && Object.keys(record(previousPayload.arguments ?? previousPayload.args)).length > 0
  ) {
    if (previousPayload.arguments) merged.arguments = previousPayload.arguments;
    if (previousPayload.args) merged.args = previousPayload.args;
  }
  if (sourceEventType === 'tool_finished') {
    for (const field of ['summary', 'message', 'label', 'status', 'state'] as const) {
      if (payload[field] === undefined) delete merged[field];
    }
  }
  return merged;
}

function attachMessage(state: RoomProjectionState, message: RoomMessageProjection): void {
  const turn = ensureTurn(state, message.turnId, message.createdAtMs);
  turn.rootId = message.rootId || turn.rootId || message.turnId;
  if (message.retryOfRootId) turn.retryOfRootId = message.retryOfRootId;
  // A message can carry a Runtime turn/wake correlation in `dispatchId`.
  // Only route decisions, explicit room_commit publications and terminal
  // events create execution obligations; otherwise a WorkDocument sync or
  // final moderator post can invent a dispatch that never becomes terminal.
  if (message.dispatchId && turn.dispatchIds?.includes(message.dispatchId)) {
    turn.dispatchParticipantIds ??= {};
    turn.dispatchParticipantIds[message.dispatchId] = message.participantId ?? '';
  }
  if (!turn.messageIds.includes(message.id)) turn.messageIds.push(message.id);
  if (message.participantId && !turn.participantIds.includes(message.participantId)) {
    turn.participantIds.push(message.participantId);
  }
  turn.updatedAtMs = Math.max(turn.updatedAtMs, message.completedAtMs ?? message.createdAtMs);
}

function detachMessage(state: RoomProjectionState, message: RoomMessageProjection): void {
  const turn = writableTurn(state, message.turnId);
  if (!turn) return;
  turn.messageIds = turn.messageIds.filter((id) => id !== message.id);
  if (turn.messageIds.length === 0 && turn.activityIds.length === 0) {
    delete state.turnsById[turn.id];
    state.turnOrder = state.turnOrder.filter((id) => id !== turn.id);
  }
}

function ensureTurn(
  state: RoomProjectionState,
  requestedTurnId: string,
  nowMs: number,
): RoomTurnProjection {
  const turnId = requestedTurnId || 'unscoped';
  let turn = writableTurn(state, turnId);
  if (!turn) {
    turn = {
      id: turnId,
      rootId: turnId,
      status: 'running',
      messageIds: [],
      activityIds: [],
      participantIds: [],
      terminalParticipantIds: [],
      failedParticipantIds: [],
      abortedParticipantIds: [],
      dispatchIds: [],
      terminalDispatchIds: [],
      failedDispatchIds: [],
      abortedDispatchIds: [],
      dispatchParticipantIds: {},
      createdAtMs: nowMs,
      updatedAtMs: nowMs,
    };
    state.turnsById[turnId] = turn;
    state.turnOrder.push(turnId);
  }
  return turn;
}

function completeParticipantTurn(
  state: RoomProjectionState,
  event: Pick<UiRoomEvent, 'turnId' | 'participantId'>,
  dispatchId: string,
  status: Extract<RoomTurnProjection['status'], 'completed' | 'failed' | 'aborted'>,
  nowMs: number,
  failure = '',
): void {
  const participantId = event.participantId ?? '';
  if (!participantId && !dispatchId) {
    const turn = ensureTurn(state, event.turnId, nowMs);
    if (
      status === 'completed'
      && hasFormalWorkResultEvidence(state, turn.rootId || turn.id)
      && !formalRootReady(state, turn)
    ) {
      turn.status = 'running';
      turn.updatedAtMs = Math.max(turn.updatedAtMs, nowMs);
      return;
    }
    completeTurn(state, event.turnId, status, nowMs, failure);
    return;
  }
  const turn = ensureTurn(state, event.turnId, nowMs);
  turn.dispatchIds ??= [];
  turn.terminalDispatchIds ??= [];
  turn.failedDispatchIds ??= [];
  turn.abortedDispatchIds ??= [];
  turn.dispatchParticipantIds ??= {};
  if (dispatchId) {
    if (!turn.dispatchIds.includes(dispatchId)) turn.dispatchIds.push(dispatchId);
    turn.dispatchParticipantIds[dispatchId] = participantId;
    if (
      turn.abortedDispatchIds.includes(dispatchId)
      || (status === 'completed' && turn.failedDispatchIds.includes(dispatchId))
    ) {
      return;
    }
    if (!turn.terminalDispatchIds.includes(dispatchId)) {
      turn.terminalDispatchIds.push(dispatchId);
    }
    if (status === 'failed' && !turn.failedDispatchIds.includes(dispatchId)) {
      turn.failedDispatchIds.push(dispatchId);
    }
    if (status === 'aborted' && !turn.abortedDispatchIds.includes(dispatchId)) {
      turn.abortedDispatchIds.push(dispatchId);
    }
  } else if (participantId) {
    for (const id of turn.dispatchIds) {
      if (turn.dispatchParticipantIds[id] !== participantId) continue;
      if (!turn.terminalDispatchIds.includes(id)) turn.terminalDispatchIds.push(id);
      if (status === 'failed' && !turn.failedDispatchIds.includes(id)) {
        turn.failedDispatchIds.push(id);
      }
      if (status === 'aborted' && !turn.abortedDispatchIds.includes(id)) {
        turn.abortedDispatchIds.push(id);
      }
    }
  }
  if (participantId && !turn.participantIds.includes(participantId)) {
    turn.participantIds.push(participantId);
  }
  turn.terminalParticipantIds ??= [];
  turn.failedParticipantIds ??= [];
  turn.abortedParticipantIds ??= [];
  const participantDispatches = turn.dispatchIds.filter(
    (id) => turn.dispatchParticipantIds?.[id] === participantId,
  );
  const participantTerminal = !dispatchId || participantDispatches.every(
    (id) => turn.terminalDispatchIds?.includes(id),
  );
  if (participantId && participantTerminal) {
    if (!turn.terminalParticipantIds.includes(participantId)) {
      turn.terminalParticipantIds.push(participantId);
    }
    if (
      participantDispatches.some((id) => turn.failedDispatchIds?.includes(id))
      || (!dispatchId && status === 'failed')
    ) {
      if (!turn.failedParticipantIds.includes(participantId)) {
        turn.failedParticipantIds.push(participantId);
      }
    }
    if (
      participantDispatches.some((id) => turn.abortedDispatchIds?.includes(id))
      || (!dispatchId && status === 'aborted')
    ) {
      if (!turn.abortedParticipantIds.includes(participantId)) {
        turn.abortedParticipantIds.push(participantId);
      }
    }
  }
  turn.updatedAtMs = Math.max(turn.updatedAtMs, nowMs);
  if (failure) turn.failure = failure;
  for (const messageId of turn.messageIds) {
    const message = state.messagesById[messageId];
    if (
      !message
      || (participantId && message.participantId !== participantId)
      || (dispatchId && message.dispatchId && message.dispatchId !== dispatchId)
    ) continue;
    state.messagesById[messageId] = {
      ...message,
      status: status === 'completed' ? 'completed' : status,
      completedAtMs: nowMs,
    };
  }
  settleTurnActivities(state, turn, status, nowMs, participantId, dispatchId);
  settleRootWhenAllDispatchesTerminal(state, turn, nowMs);
}

function settleRootWhenAllDispatchesTerminal(
  state: RoomProjectionState,
  turn: RoomTurnProjection,
  nowMs: number,
): void {
  const dispatchIds = turn.dispatchIds ?? [];
  const terminalDispatchIds = new Set(turn.terminalDispatchIds ?? []);
  if (
    dispatchIds.length === 0
    || !dispatchIds.every((dispatchId) => terminalDispatchIds.has(dispatchId))
  ) {
    turn.status = 'running';
    return;
  }
  const failedDispatchIds = new Set(turn.failedDispatchIds ?? []);
  const abortedDispatchIds = new Set(turn.abortedDispatchIds ?? []);
  const status = dispatchIds.some((dispatchId) => failedDispatchIds.has(dispatchId))
    ? 'failed'
    : dispatchIds.some((dispatchId) => abortedDispatchIds.has(dispatchId))
      ? 'aborted'
      : 'completed';
  if (status === 'completed' && !formalRootReady(state, turn)) {
    turn.status = 'running';
    return;
  }
  completeTurn(state, turn.id, status, nowMs, turn.failure);
}
function formalRootReady(state: RoomProjectionState, turn: RoomTurnProjection): boolean {
  const rootId = turn.rootId || turn.id;
  if (!hasFormalWorkResultEvidence(state, rootId)) return true;
  const dispatchIds = turn.dispatchIds ?? [];
  if (
    dispatchIds.length === 0
    || !dispatchIds.every((dispatchId) => turn.terminalDispatchIds?.includes(dispatchId))
  ) return false;
  return hasModeratorFinalReport(state, rootId);
}

function hasFormalWorkResultEvidence(
  state: RoomProjectionState,
  rootId: string,
): boolean {
  return Object.values(state.messagesById).some((message) => (
    message.role === 'assistant'
    && message.projectionKind === 'post'
    && message.postKind === 'work_result'
    && (message.rootId === rootId || message.turnId === rootId)
  ));
}

function hasModeratorFinalReport(
  state: RoomProjectionState,
  rootId: string,
): boolean {
  return Object.values(state.messagesById).some((message) => (
    message.role === 'assistant'
    && message.projectionKind === 'post'
    && message.postKind === 'result'
    && (message.rootId === rootId || message.turnId === rootId)
    && (
      !state.moderatorParticipantId
      || message.participantId === state.moderatorParticipantId
    )
  ));
}

function completeTurn(
  state: RoomProjectionState,
  turnId: string,
  status: Extract<RoomTurnProjection['status'], 'completed' | 'failed' | 'aborted'>,
  nowMs: number,
  failure = '',
): void {
  const turn = ensureTurn(state, turnId, nowMs);
  const settledParticipants = new Set(turn.terminalParticipantIds ?? []);
  const settledDispatches = new Set(turn.terminalDispatchIds ?? []);
  const failedParticipants = new Set(turn.failedParticipantIds ?? []);
  const abortedParticipants = new Set(turn.abortedParticipantIds ?? []);
  const failedDispatches = new Set(turn.failedDispatchIds ?? []);
  const abortedDispatches = new Set(turn.abortedDispatchIds ?? []);
  const unresolvedParticipants = turn.participantIds.filter(
    (participantId) => !settledParticipants.has(participantId),
  );
  const unresolvedDispatches = (turn.dispatchIds ?? []).filter(
    (dispatchId) => !settledDispatches.has(dispatchId),
  );
  if (status === 'failed') {
    unresolvedParticipants.forEach((participantId) => failedParticipants.add(participantId));
    unresolvedDispatches.forEach((dispatchId) => failedDispatches.add(dispatchId));
  } else if (status === 'aborted') {
    unresolvedParticipants.forEach((participantId) => abortedParticipants.add(participantId));
    unresolvedDispatches.forEach((dispatchId) => abortedDispatches.add(dispatchId));
  }
  turn.status = status;
  turn.rootTerminalAtMs = nowMs;
  turn.updatedAtMs = nowMs;
  turn.terminalParticipantIds = [...turn.participantIds];
  turn.failedParticipantIds = [...failedParticipants];
  turn.abortedParticipantIds = [...abortedParticipants];
  turn.terminalDispatchIds = [...(turn.dispatchIds ?? [])];
  turn.failedDispatchIds = [...failedDispatches];
  turn.abortedDispatchIds = [...abortedDispatches];
  if (failure) turn.failure = failure;
  settleTurnActivities(state, turn, status, nowMs);
  for (const messageId of turn.messageIds) {
    const message = state.messagesById[messageId];
    if (!message || message.role === 'user' || message.status === 'completed') continue;
    state.messagesById[messageId] = {
      ...message,
      status: status === 'completed' ? 'completed' : status,
      completedAtMs: nowMs,
    };
  }
}
function settleTurnActivities(
  state: RoomProjectionState,
  turn: RoomTurnProjection,
  fallbackStatus: Extract<RoomTurnProjection['status'], 'completed' | 'failed' | 'aborted'>,
  nowMs: number,
  participantId = '',
  dispatchId = '',
): void {
  const settleWaiting = (!participantId && !dispatchId) || fallbackStatus !== 'completed';
  for (const activityId of turn.activityIds) {
    const activity = state.activitiesById[activityId];
    if (
      !activity
      || (
        activity.status !== 'running'
        && !(settleWaiting && activity.status === 'waiting')
      )
    ) continue;
    const activityParticipantId = activity.participantId ?? '';
    const activityDispatchId = text(activity.payload.dispatchId);
    if (participantId && activityParticipantId !== participantId) continue;
    if (dispatchId && activityDispatchId !== dispatchId) continue;

    let status: Extract<
      RoomActivityProjection['status'],
      'completed' | 'failed' | 'aborted'
    > = fallbackStatus;
    if (!participantId && !dispatchId) {
      if (activityDispatchId) {
        if (turn.failedDispatchIds?.includes(activityDispatchId)) status = 'failed';
        else if (turn.abortedDispatchIds?.includes(activityDispatchId)) status = 'aborted';
        else if (turn.terminalDispatchIds?.includes(activityDispatchId)) status = 'completed';
      } else if (activityParticipantId) {
        if (turn.failedParticipantIds?.includes(activityParticipantId)) status = 'failed';
        else if (turn.abortedParticipantIds?.includes(activityParticipantId)) status = 'aborted';
        else if (turn.terminalParticipantIds?.includes(activityParticipantId)) status = 'completed';
      }
    }
    state.activitiesById[activityId] = {
      ...activity,
      status,
      updatedAtMs: Math.max(activity.updatedAtMs ?? activity.createdAtMs, nowMs),
    };
  }
}

function appendDiagnostic(state: RoomProjectionState, diagnostic: ProjectionDiagnostic): void {
  state.diagnostics.push(diagnostic);
  if (state.diagnostics.length > diagnosticLimit) {
    state.diagnostics.splice(0, state.diagnostics.length - diagnosticLimit);
  }
}

function messageText(message: UiAgentMessage): string {
  return message.blocks
    .filter((block) => block.type === 'text' || block.type === 'code')
    .map((block) => text(record(block.data).text ?? record(block.data).code))
    .filter(Boolean)
    .join('\n');
}

function executionMessageId(
  event: UiRoomEvent,
  payload: Record<string, unknown>,
): string {
  const messageId = text(payload.messageId);
  const dispatchId = text(payload.dispatchId);
  const sourceLoopId = text(payload.sourceLoopId);
  if (!dispatchId) {
    return [
      messageId || `${event.turnId}:${event.participantId ?? 'participant'}:assistant`,
      sourceLoopId,
    ].filter(Boolean).join('\u001f');
  }
  const rootId = text(payload.rootId) || event.turnId;
  const participantId = (event.participantId ?? event.sourceSessionId) || 'participant';
  return [
    'room-execution',
    rootId,
    participantId,
    dispatchId,
    sourceLoopId,
    messageId || 'assistant',
  ].filter(Boolean).join('\u001f');
}

function cloneState(state: RoomProjectionState): RoomProjectionState {
  return {
    ...state,
    messagesById: { ...state.messagesById },
    messageOrder: [...state.messageOrder],
    activitiesById: { ...state.activitiesById },
    activityOrder: [...state.activityOrder],
    // Match Pi's component-local updates: only the active turn is copied when
    // an event mutates it, while completed history stays referentially stable.
    turnsById: { ...state.turnsById },
    turnOrder: [...state.turnOrder],
    optimisticByClientMessageId: { ...state.optimisticByClientMessageId },
    diagnostics: [...state.diagnostics],
    ...(state.gap ? { gap: { ...state.gap } } : {}),
  };
}

function canStartRoomDeltaBatch(
  state: RoomProjectionState,
  event: UiRoomEvent,
): boolean {
  return (
    event.eventType === 'participant_delta'
    && event.roomId === state.roomId
    && !state.needsSnapshot
    && event.sequence > state.lastSequence
    && (state.lastSequence === 0 || event.sequence === state.lastSequence + 1)
  );
}

function canMergeRoomDelta(
  previous: UiRoomEvent,
  candidate: UiRoomEvent,
): boolean {
  if (
    candidate.eventType !== 'participant_delta'
    || candidate.roomId !== previous.roomId
    || candidate.turnId !== previous.turnId
    || candidate.topicId !== previous.topicId
    || candidate.participantId !== previous.participantId
    || candidate.sourceSessionId !== previous.sourceSessionId
    || candidate.sequence !== previous.sequence + 1
    || candidate.payload.replaceBlock === true
    || candidate.payload.replaceContent === true
  ) {
    return false;
  }
  return sameDeltaField(previous.payload, candidate.payload, 'rootId')
    && sameDeltaField(previous.payload, candidate.payload, 'dispatchId')
    && sameDeltaField(previous.payload, candidate.payload, 'messageId')
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
  state: RoomProjectionState,
  turnId: string,
): RoomTurnProjection | undefined {
  const current = state.turnsById[turnId];
  if (!current) return undefined;
  const copy: RoomTurnProjection = {
    ...current,
    messageIds: [...current.messageIds],
    activityIds: [...current.activityIds],
    participantIds: [...current.participantIds],
    terminalParticipantIds: [...(current.terminalParticipantIds ?? [])],
    failedParticipantIds: [...(current.failedParticipantIds ?? [])],
    abortedParticipantIds: [...(current.abortedParticipantIds ?? [])],
    dispatchIds: [...(current.dispatchIds ?? [])],
    terminalDispatchIds: [...(current.terminalDispatchIds ?? [])],
    failedDispatchIds: [...(current.failedDispatchIds ?? [])],
    abortedDispatchIds: [...(current.abortedDispatchIds ?? [])],
    dispatchParticipantIds: { ...(current.dispatchParticipantIds ?? {}) },
  };
  state.turnsById[turnId] = copy;
  return copy;
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function isExecutionEventAfterRootTerminal(
  state: RoomProjectionState,
  event: UiRoomEvent,
  payload: Record<string, unknown>,
): boolean {
  if (![
    'participant_delta',
    'participant_message',
    'room_post',
    'route_decision',
    'participant_status',
    'participant_activity',
    'turn_completed',
    'turn_failed',
  ].includes(event.eventType)) return false;
  const post = record(payload.post);
  const rootId = text(payload.rootId) || text(post.rootId) || event.turnId;
  return state.turnsById[rootId]?.rootTerminalAtMs != null;
}
function isAwaitedRoomFinal(
  state: RoomProjectionState,
  event: UiRoomEvent,
  payload: Record<string, unknown>,
): boolean {
  if (event.eventType !== 'room_post') return false;
  const post = record(payload.post);
  if (text(post.kind) !== 'result') return false;
  const rootId = text(payload.rootId) || text(post.rootId) || event.turnId;
  const turn = state.turnsById[rootId];
  if (
    !turn
    || turn.status !== 'completed'
    || turn.rootTerminalAtMs == null
    || !hasFormalWorkResultEvidence(state, rootId)
    || hasModeratorFinalReport(state, rootId)
  ) return false;
  return !state.moderatorParticipantId
    || event.participantId === state.moderatorParticipantId;
}

function isUnroutedParticipantSessionEvent(
  state: RoomProjectionState,
  event: UiRoomEvent,
  payload: Record<string, unknown>,
): boolean {
  if (![
    'participant_delta',
    'participant_message',
    'participant_status',
    'participant_activity',
    'turn_completed',
    'turn_failed',
  ].includes(event.eventType)) return false;
  if (!event.participantId || !event.turnId) return false;
  if (!text(payload.sourceEventId) || text(payload.dispatchId)) return false;
  const rootId = text(payload.rootId) || event.turnId;
  return state.turnsById[rootId] == null;
}

function publicRoomPayload(value: unknown): Record<string, unknown> {
  const envelope = record(value);
  if (
    typeof envelope.sourceEventId === 'string'
    && typeof envelope.sourceEventType === 'string'
    && typeof envelope.data === 'object'
    && envelope.data !== null
    && !Array.isArray(envelope.data)
  ) {
    const data = record(envelope.data);
    const rootId = text(data.rootId) || text(envelope.rootId);
    const dispatchId = text(data.dispatchId) || text(envelope.dispatchId);
    const messageId = text(data.messageId) || text(envelope.messageId);
    const blockId = text(data.blockId) || text(envelope.blockId);
    return {
      ...data,
      ...(rootId ? { rootId } : {}),
      ...(dispatchId ? { dispatchId } : {}),
      ...(messageId ? { messageId } : {}),
      ...(blockId ? { blockId } : {}),
      sourceEventId: text(data.sourceEventId) || envelope.sourceEventId,
      sourceEventType: text(data.sourceEventType) || envelope.sourceEventType,
    };
  }
  return envelope;
}

function roomParticipantProgressKind(
  eventKind: string,
  sourceEventType: string,
  activityKind: string,
): RoomParticipantPublicProgressKind {
  if (sourceEventType === 'reasoning_summary') return 'reasoning';
  if (['current_progress', 'progress'].includes(sourceEventType)) return 'progress';
  if (sourceEventType.startsWith('tool_')) return 'tool';
  if (eventKind === 'route_decision') return 'dispatch';
  if (eventKind === 'participant_status') return 'status';
  if (activityKind === 'intercom') return 'dispatch';
  return 'activity';
}

function roomParticipantProgressSummary(
  value: string,
  sourceEventType: string,
  eventKind: string,
): string {
  const summary = value.trim();
  if (
    summary
    && summary !== sourceEventType
    && summary !== eventKind
    && !/\b(?:participant|route|tool|turn)_[a-z_]+\b/iu.test(summary)
  ) return summary;
  if (sourceEventType === 'reasoning_summary') return '工作摘要已更新';
  if (['current_progress', 'progress'].includes(sourceEventType)) return '工作进度已更新';
  if (sourceEventType.startsWith('tool_')) return '工具进度已更新';
  if (eventKind === 'route_decision') return '已确认本轮分工';
  if (eventKind === 'participant_status') return '伙伴状态已更新';
  return '协作进度已更新';
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}
