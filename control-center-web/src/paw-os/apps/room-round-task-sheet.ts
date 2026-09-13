import type {
  RoomActivityProjection,
  RoomMessageProjection,
  RoomProjectionState,
  RoomTurnProjection,
} from '@/contracts/room-reducer';
import { roomCollaborationRoleLabel } from '@/features/rooms/room-copy';
import {
  selectPublicRoomTurnOrder,
  selectRoomTurnExecution,
  type RoomExecutionLane,
} from '@/features/rooms/runtime/room-execution-lanes';
import type { RoomParticipant, RoomSummary, RoomWorkItem } from '@/features/rooms/room-types';
import { roomFocusCelestialName } from './room-focus-projection';

export type RoomRoundRowState = 'queued' | 'waiting' | 'running' | 'blocked' | 'completed' | 'failed' | 'aborted';

export interface RoomRoundRowEvent {
  id: string;
  kind: 'activity' | 'message';
  status: RoomRoundRowState;
  summary: string;
  updatedAtMs: number;
}

export interface RoomRoundTaskRow {
  key: string;
  participantId: string;
  sessionId: string;
  displayName: string;
  celestialName: string;
  role: string;
  /** True only when this logical round contains authoritative work for the planet. */
  assigned: boolean;
  state: RoomRoundRowState;
  task: string;
  /** Full current assignment, retained for explicit disclosure. */
  taskBody?: string;
  latestProgress: string;
  blockerReason?: string;
  blockerNextStep?: string;
  /** The authoritative blocked WorkItem to pass to the Room resume command. */
  blockedWorkItemId?: string;
  result?: string;
  /** Full public moderator prose, including a still-streaming report. This
   * carries content only and never confers final-result status. */
  report?: string;
  /** Exact typed moderator publication for a completed current Room turn. */
  finalMessageId?: string;
  /** Explicit kind of the latest public moderator assistant RoomPost. */
  postKind?: RoomMessageProjection['postKind'];
  evidenceRefs: string[];
  history: RoomRoundRowEvent[];
  updatedAtMs: number;
}

export interface RoomRoundTaskSheet {
  /** Stable identity of the original user request, retained across retries. */
  id: string;
  /** Current attempt projected into this logical round. */
  turnId: string;
  objective: string;
  status: RoomTurnProjection['status'];
  rows: RoomRoundTaskRow[];
  createdAtMs: number;
  updatedAtMs: number;
}

const MAX_ROW_HISTORY = 40;
const MAX_ROW_EVIDENCE_REFS = 40;

/**
 * UR-170/172: project the public Room event stream into one stable sheet per
 * logical user request and one stable row per planet. The projection reads
 * the same authoritative turns/lanes as the public transcript; it does not
 * create a second execution state or infer completion from prose.
 */
export function selectRoomRoundTaskSheets(
  room: RoomSummary,
  projection: RoomProjectionState,
): RoomRoundTaskSheet[] {
  return selectPublicRoomTurnOrder(projection).flatMap((turnId) => {
    const turn = projection.turnsById[turnId];
    if (!turn) return [];
    const sheetId = logicalRoomRootId(projection, turnId);
    const execution = selectRoomTurnExecution(projection, turnId);
    const historyTurnIds = logicalRoomTurnIds(projection, sheetId);
    const historyTurns = historyTurnIds
      .map((historyTurnId) => projection.turnsById[historyTurnId])
      .filter((historyTurn): historyTurn is RoomTurnProjection => Boolean(historyTurn));
    const historyLanes = historyTurnIds.flatMap((historyTurnId) => (
      selectRoomTurnExecution(projection, historyTurnId).lanes
    ));
    const userMessage = latestUserMessage(execution.userMessageIds, projection)
      ?? latestUserMessage(projection.turnsById[sheetId]?.messageIds ?? [], projection);
    const participants = sheetParticipants(room.participants, execution.lanes);
    return [{
      id: sheetId,
      turnId,
      objective: compactText(userMessage?.text ?? '') || '继续当前协作',
      status: turn.status,
      rows: participants.map((participant) => roundRow({
        executionLanes: execution.lanes,
        historyLanes,
        historyTurns,
        participant,
        projection,
        room,
        sheetId,
        turn,
        turnId,
      })),
      createdAtMs: projection.turnsById[sheetId]?.createdAtMs ?? turn.createdAtMs,
      updatedAtMs: turn.updatedAtMs,
    }];
  });
}

function roundRow({
  executionLanes,
  historyLanes,
  historyTurns,
  participant,
  projection,
  room,
  sheetId,
  turn,
  turnId,
}: {
  executionLanes: RoomExecutionLane[];
  historyLanes: RoomExecutionLane[];
  historyTurns: RoomTurnProjection[];
  participant: RoomParticipant;
  projection: RoomProjectionState;
  room: RoomSummary;
  sheetId: string;
  turn: RoomTurnProjection;
  turnId: string;
}): RoomRoundTaskRow {
  const lanes = executionLanes.filter((lane) => (
    lane.participantId === participant.id
    || (!lane.participantId && lane.sourceSessionId === participant.sessionId)
  ));
  const historicalLanes = historyLanes.filter((lane) => (
    lane.participantId === participant.id
    || (!lane.participantId && lane.sourceSessionId === participant.sessionId)
  ));
  const currentActivities = lanes.flatMap((lane) => lane.activities);
  const activities = historicalLanes.flatMap((lane) => lane.activities);
  const currentMessages = assistantMessagesForLanes(lanes, projection).filter((message) => (
    message.turnId === turnId
    && (!message.rootId || message.rootId === turnId || message.rootId === turn.rootId)
  ));
  const messages = assistantMessagesForLanes(historicalLanes, projection);
  const historyTurnIds = historyTurns.map((historyTurn) => historyTurn.id);
  const roundWorkItems = (room.workItems ?? []).filter((work) => (
    historyTurnIds.includes(work.rootTurnId)
  ));
  const workItems = matchingWorkItems(room.workItems ?? [], participant.id, historyTurnIds);
  const currentWorkItems = matchingWorkItems(room.workItems ?? [], participant.id, [turnId]);
  const participantHistoryTurns = historyTurns.filter((historyTurn) => (
    historyTurn.participantIds.includes(participant.id)
    || historicalLanes.some((lane) => (
      lane.rootId === (historyTurn.rootId || historyTurn.id)
      || lane.rootId === historyTurn.id
    ))
  ));
  const history = rowHistory(activities, messages, participantHistoryTurns, workItems);
  const currentTurns = participantHistoryTurns.filter((historyTurn) => historyTurn.id === turnId);
  const currentHistory = rowHistory(currentActivities, currentMessages, currentTurns, currentWorkItems);
  const latestCurrent = currentHistory.at(-1);
  const orderedCurrentMessages = [...currentMessages]
    .sort(compareRoomMessages)
    .reverse();
  const latestPublicMessage = orderedCurrentMessages.find((message) => Boolean(message.text.trim()));
  const latestCompletedMessage = orderedCurrentMessages
    .find((message) => message.status === 'completed' && Boolean(message.text.trim()));
  const isModerator = participant.id === room.moderatorParticipantId;
  const coordinatorResultMessage = isModerator && turn.status === 'completed'
    ? orderedCurrentMessages.find((message) => (
      message.status === 'completed'
      && message.postKind === 'result'
      && Boolean(message.text.trim())
    ))
    : undefined;
  const resultMessage = isModerator ? coordinatorResultMessage : latestCompletedMessage;
  const workResult = [...currentWorkItems]
    .reverse()
    .map((work) => work.resultSummary.trim())
    .find(Boolean);
  const blockerReason = latestBlockerText(currentWorkItems, 'reason');
  const blockerNextStep = latestBlockerText(currentWorkItems, 'nextStep');
  const blockedWorkItemId = [...currentWorkItems]
    .reverse()
    .find((work) => work.state === 'blocked')?.id;
  const eventAssigned = turn.participantIds.includes(participant.id)
    || lanes.length > 0
    || currentActivities.length > 0
    || currentMessages.length > 0
    || currentWorkItems.length > 0;
  /* A facilitator can stay live and accountable while another planet owns the
   * only WorkItem. Accountability alone is coordination, not a second task
   * assignment. Keep event-only collaborators that have no delegated
   * accountability record (including unfinished lanes beside a submitted
   * result), while letting an explicit owner remain authoritative. */
  const delegatedAccountabilityOnly = workItems.length === 0 && roundWorkItems.some((work) => (
    work.accountableParticipantId === participant.id
    && assignedWorkParticipantId(work) !== participant.id
  ));
  /* Keep event-backed work visible even when the coordinator is the only
     participant. The view layer decides whether that coordinator event is a
     standalone task or a synthesis card once it knows whether worker rows
     exist; suppress only the explicit facilitator-accountability bookkeeping
     case above. */
  const assigned = workItems.length > 0 || (eventAssigned && !delegatedAccountabilityOnly);
  const task = latestTask(currentActivities)
    || currentWorkItems.find((work) => Boolean(work.objective.trim()))?.objective.trim()
    || (assigned ? latestUserMessage(turn.messageIds, projection)?.text.trim() : '')
    || `${roomCollaborationRoleLabel(participant.collaborationRole)} · 等待本轮分工`;
  const participantState = rowState(turn, participant.id, lanes, currentActivities, currentMessages, currentWorkItems);
  // A completed facilitator step does not complete the Room's current answer.
  // Keep explicit blockers/failures actionable while the Room is live; once the
  // current Room turn ends, its terminal verdict owns the coordinator surface.
  const state = isModerator && eventAssigned
    ? turn.status === 'running' && participantState === 'completed'
      ? 'running'
      : ['completed', 'failed', 'aborted'].includes(turn.status)
        ? turn.status
        : participantState
    : participantState;
  const completedProgress = compactMarkdown(
    workResult
      || resultMessage?.text.trim()
      || (latestCurrent && !['queued', 'waiting', 'running'].includes(latestCurrent.status)
        ? latestCurrent.summary
        : ''),
  ) || progressFallback('completed');
  return {
    key: `${sheetId}:${participant.id}`,
    participantId: participant.id,
    sessionId: participant.sessionId,
    displayName: participant.displayName,
    celestialName: roomFocusCelestialName(participant.ordinal),
    role: roomCollaborationRoleLabel(participant.collaborationRole),
    assigned,
    state,
    task: compactMarkdown(task),
    taskBody: task.replace(/\r\n?/gu, '\n'),
    latestProgress: state === 'blocked' && blockerReason
      ? blockerReason
      : state === 'completed'
        ? completedProgress
        : latestCurrent?.summary || progressFallback(state),
    ...(blockerReason ? { blockerReason } : {}),
    ...(blockerNextStep ? { blockerNextStep } : {}),
    ...(blockedWorkItemId ? { blockedWorkItemId } : {}),
    ...(isModerator && latestPublicMessage?.postKind ? { postKind: latestPublicMessage.postKind } : {}),
    ...(isModerator && latestPublicMessage
      ? { report: latestPublicMessage.text.trim().replace(/\r\n?/gu, '\n') }
      : {}),
    ...(coordinatorResultMessage ? { finalMessageId: coordinatorResultMessage.id } : {}),
    ...(resultMessage?.text.trim() || (!isModerator && workResult)
      // Result surfaces are the answer, not a list preview. Keep the complete
      // Markdown; only task/progress summaries above have a compact budget.
      ? { result: (resultMessage?.text.trim() || workResult || '').replace(/\r\n?/gu, '\n') }
      : {}),
    evidenceRefs: unique(workItems.flatMap((work) => [...work.artifactRefs, ...work.evidenceRefs]))
      .slice(-MAX_ROW_EVIDENCE_REFS),
    history,
    updatedAtMs: latestCurrent?.updatedAtMs ?? turn.updatedAtMs,
  };
}

function rowState(
  turn: RoomTurnProjection,
  participantId: string,
  lanes: RoomExecutionLane[],
  activities: RoomActivityProjection[],
  messages: RoomMessageProjection[],
  workItems: RoomWorkItem[],
): RoomRoundRowState {
  if (turn.failedParticipantIds?.includes(participantId)) return 'failed';
  if (turn.abortedParticipantIds?.includes(participantId)) return 'aborted';

  if (workItems.some((work) => work.state === 'failed')) return 'failed';
  if (workItems.some((work) => work.state === 'cancelled')) return 'aborted';
  if (workItems.some((work) => work.state === 'blocked')) return 'blocked';
  if (workItems.length && workItems.every((work) => work.state === 'done')) return 'completed';

  if (turn.terminalParticipantIds?.includes(participantId)) return 'completed';

  const participates = turn.participantIds.includes(participantId) || lanes.length > 0;
  if (participates && turn.status === 'completed') return 'completed';
  if (participates && turn.status === 'failed') return 'failed';
  if (participates && turn.status === 'aborted') return 'aborted';
  /* A failed Tool call is a recoverable event inside an active Pi turn, not a
     terminal verdict on the planet. Keep its red receipt in row history, but
     let the current turn/work lifecycle own the headline state. */
  if (participates && turn.status === 'running') return 'running';
  if (workItems.some((work) => work.state === 'active')) return 'running';
  if (workItems.some((work) => work.state === 'review')) return 'waiting';
  if (workItems.some((work) => work.state === 'queued')) return 'queued';

  const statuses = [
    ...activities.map((activity) => activity.status),
    ...messages.map((message) => message.status),
  ];
  /* Activity/message failure is deliberately not a row-level verdict. A
     completed or still-live turn can contain a rejected tool call that Pi
     recovered from; the authoritative turn/WorkItem state above owns the
     headline. This prevents every planet from becoming "需要关注" merely
     because one receipt is red. */
  if (statuses.includes('running') || statuses.includes('streaming')) return 'running';
  if (statuses.includes('waiting')) return 'waiting';

  if (participates && turn.status === 'running') return 'running';
  return turn.status === 'queued' ? 'queued' : 'waiting';
}

function latestBlockerText(workItems: RoomWorkItem[], key: 'reason' | 'nextStep'): string {
  for (const work of [...workItems].reverse()) {
    const value = text(work.blocker?.[key]);
    if (value) return compactMarkdown(value);
  }
  return '';
}

function rowHistory(
  activities: RoomActivityProjection[],
  messages: RoomMessageProjection[],
  turns: RoomTurnProjection[] = [],
  workItems: RoomWorkItem[] = [],
): RoomRoundRowEvent[] {
  const events = [
    ...activities.flatMap((activity): RoomRoundRowEvent[] => {
      const publicHistory = activityProgressHistory(activity);
      if (publicHistory !== null) return publicHistory;
      const summary = compactMarkdown(activity.summary);
      if (!summary) return [];
      return [{
        id: activity.id,
        kind: 'activity',
        status: activityStatus(activity.status),
        summary,
        updatedAtMs: activity.updatedAtMs ?? activity.createdAtMs,
      }];
    }),
    ...messages.flatMap((message): RoomRoundRowEvent[] => {
      const summary = compactMarkdown(message.text);
      if (!summary) return [];
      return [{
        id: message.id,
        kind: 'message',
        status: messageStatus(message.status),
        summary,
        updatedAtMs: message.completedAtMs ?? message.createdAtMs,
      }];
    }),
    ...turns.flatMap((turn): RoomRoundRowEvent[] => {
      const summary = compactMarkdown(turn.failure ?? '');
      if (!summary) return [];
      return [{
        id: `${turn.id}:failure`,
        kind: 'activity',
        status: 'failed',
        summary,
        updatedAtMs: turn.updatedAtMs,
      }];
    }),
    ...workItems.flatMap((work): RoomRoundRowEvent[] => {
      const summary = compactMarkdown(text(work.blocker?.reason));
      if (!summary) return [];
      return [{
        id: `${work.id}:blocker`,
        kind: 'activity',
        status: 'blocked',
        summary,
        updatedAtMs: work.updatedAtMs,
      }];
    }),
  ].sort(compareRowEvents);
  const uniqueEvents = new Map<string, RoomRoundRowEvent>();
  for (const event of events) {
    const previous = uniqueEvents.get(event.id);
    if (!previous || event.updatedAtMs >= previous.updatedAtMs) {
      uniqueEvents.set(event.id, event);
    }
  }
  return [...uniqueEvents.values()]
    .sort(compareRowEvents)
    .slice(-MAX_ROW_HISTORY);
}

function compareRowEvents(left: RoomRoundRowEvent, right: RoomRoundRowEvent): number {
  return left.updatedAtMs - right.updatedAtMs
    || left.id.localeCompare(right.id)
    || left.summary.localeCompare(right.summary);
}

function assistantMessagesForLanes(
  lanes: RoomExecutionLane[],
  projection: RoomProjectionState,
): RoomMessageProjection[] {
  return unique(lanes.flatMap((lane) => lane.messageIds))
    .map((messageId) => projection.messagesById[messageId])
    .filter((message): message is RoomMessageProjection => Boolean(message && message.role === 'assistant'))
    .flatMap((message) => {
      // The flattened Room message may come from an older snapshot. When its
      // typed blocks are available, only actual public answer text may enter
      // reply/result surfaces; progress, reasoning and Tool receipts keep their
      // own activity projection. A private block never becomes a public reply.
      if (!message.message) return message.text.trim() ? [message] : [];
      const publicText = message.message.blocks
        .filter((block) => (block.type === 'text' || block.type === 'code')
          && block.visibility !== 'private_session'
          && !['reasoning_summary', 'tool_call', 'tool_result', 'status', 'progress'].includes(block.presentationKind))
        .map((block) => text(block.data.text) || text(block.data.code))
        .filter(Boolean)
        .join('\n\n');
      return publicText ? [{ ...message, text: publicText }] : [];
    })
    .sort(compareRoomMessages);
}

function compareRoomMessages(
  left: RoomMessageProjection,
  right: RoomMessageProjection,
): number {
  const leftSequence = left.chronology?.roomEventSequence ?? left.sequence;
  const rightSequence = right.chronology?.roomEventSequence ?? right.sequence;
  if (leftSequence !== undefined || rightSequence !== undefined) {
    if (leftSequence === undefined) return 1;
    if (rightSequence === undefined) return -1;
    const sequenceOrder = leftSequence - rightSequence;
    if (sequenceOrder !== 0) return sequenceOrder;
  }
  const timeOrder = (
    left.chronology?.createdAtMs ?? left.createdAtMs
  ) - (
    right.chronology?.createdAtMs ?? right.createdAtMs
  );
  if (timeOrder !== 0) return timeOrder;
  const orderKeyOrder = (left.chronology?.orderKey ?? '').localeCompare(
    right.chronology?.orderKey ?? '',
  );
  return orderKeyOrder || left.id.localeCompare(right.id);
}

/**
 * Coalesced public progress keeps a bounded source-event ledger in the
 * reducer. Expand that ledger inside the same stable planet row instead of
 * restoring each update as a top-level Room message. `null` means this
 * activity has no ledger and should use its current summary; an empty array
 * means a ledger exists but contains no public text, so machine enum fallbacks
 * must stay hidden.
 */
function activityProgressHistory(
  activity: RoomActivityProjection,
): RoomRoundRowEvent[] | null {
  const value = activity.payload.progressHistory;
  if (!Array.isArray(value)) return null;
  return value.flatMap((entry, index): RoomRoundRowEvent[] => {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) return [];
    const item = entry as Record<string, unknown>;
    const summary = compactMarkdown(
      text(item.summary) || text(item.message) || text(item.label),
    );
    if (!summary) return [];
    const timestamp = finiteTimestamp(item.createdAtMs)
      ?? activity.updatedAtMs
      ?? activity.createdAtMs;
    const sourceId = text(item.eventId) || text(item.sourceEventId);
    return [{
      id: sourceId || `${activity.id}:history:${index}`,
      kind: 'activity',
      status: rowEventStatus(item.status, activityStatus(activity.status)),
      summary,
      updatedAtMs: timestamp,
    }];
  });
}

function rowEventStatus(value: unknown, fallback: RoomRoundRowState): RoomRoundRowState {
  const status = text(value);
  if (status === 'streaming') return 'running';
  if ([
    'queued',
    'waiting',
    'running',
    'blocked',
    'completed',
    'failed',
    'aborted',
  ].includes(status)) return status as RoomRoundRowState;
  return fallback;
}

function finiteTimestamp(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? value
    : null;
}

function latestTask(activities: RoomActivityProjection[]): string {
  for (const activity of [...activities].reverse()) {
    const task = text(activity.payload.task)
      || text(activity.payload.objective);
    // Routing roles and failure reasons describe execution, not the user's
    // assignment. The original payload remains available in the full trace.
    if (['facilitator', 'moderator', 'worker', 'reviewer'].includes(task.trim())) continue;
    if (task) return task;
  }
  return '';
}

function matchingWorkItems(
  workItems: RoomWorkItem[],
  participantId: string,
  turnIds: readonly string[],
): RoomWorkItem[] {
  const matchingTurnIds = new Set(turnIds);
  const matching = workItems.filter((work) => (
    matchingTurnIds.has(work.rootTurnId)
    && assignedWorkParticipantId(work) === participantId
  ));
  const uniqueById = new Map(matching.map((work) => [work.id, work]));
  return [...uniqueById.values()].sort(compareWorkItems);
}

function assignedWorkParticipantId(work: RoomWorkItem): string {
  return work.currentOwnerParticipantId
    || work.offeredToParticipantId
    || work.accountableParticipantId;
}

function compareWorkItems(left: RoomWorkItem, right: RoomWorkItem): number {
  const timeOrder = left.updatedAtMs - right.updatedAtMs
    || left.createdAtMs - right.createdAtMs;
  return timeOrder || left.id.localeCompare(right.id);
}

function sheetParticipants(
  roomParticipants: RoomParticipant[],
  lanes: RoomExecutionLane[],
): RoomParticipant[] {
  const byId = new Map(roomParticipants.map((participant) => [participant.id, participant]));
  for (const lane of lanes) {
    if (!lane.participantId || byId.has(lane.participantId)) continue;
    byId.set(lane.participantId, {
      id: lane.participantId,
      sessionId: lane.sourceSessionId,
      roleId: '',
      roleVersion: '',
      displayName: lane.participantId,
      status: 'removed',
      ordinal: byId.size,
    });
  }
  return [...byId.values()].sort((left, right) => (
    left.ordinal - right.ordinal || left.id.localeCompare(right.id)
  ));
}

function logicalRoomRootId(projection: RoomProjectionState, turnId: string): string {
  let current = turnId;
  const visited = new Set<string>();
  while (!visited.has(current)) {
    visited.add(current);
    const turn = projection.turnsById[current];
    if (turn?.logicalRootId) return turn.logicalRootId;
    const parent = turn?.retryOfRootId;
    if (!parent) return current;
    /* A bounded snapshot may retain only the retry event. The explicit
     * retryOfRootId is still a reliable canonical sheet identity; do not
     * collapse it into an unrelated new user turn just because the parent
     * record is outside the retained prefix. */
    if (!projection.turnsById[parent]) return parent;
    current = parent;
  }
  return current;
}

function logicalRoomTurnIds(
  projection: RoomProjectionState,
  sheetId: string,
): string[] {
  const turnIds = unique(projection.turnOrder.filter((turnId) => (
    logicalRoomRootId(projection, turnId) === sheetId
  )));
  if (!turnIds.includes(sheetId) && projection.turnsById[sheetId]) {
    turnIds.unshift(sheetId);
  }
  return turnIds;
}

function latestUserMessage(
  messageIds: string[],
  projection: RoomProjectionState,
): RoomMessageProjection | undefined {
  return [...messageIds]
    .reverse()
    .map((messageId) => projection.messagesById[messageId])
    .find((message) => message?.role === 'user');
}

function activityStatus(status: RoomActivityProjection['status']): RoomRoundRowState {
  return status === 'waiting' ? 'waiting' : status;
}

function messageStatus(status: RoomMessageProjection['status']): RoomRoundRowState {
  if (status === 'streaming') return 'running';
  return status;
}

export function progressFallback(state: RoomRoundRowState): string {
  return ({
    queued: '等待本轮开始',
    waiting: '等待本轮分工或公开进展',
    running: '正在执行，等待公开进展',
    blocked: '已阻塞，等待恢复操作',
    completed: '本轮工作已结束',
    failed: '本轮需要处理失败',
    aborted: '本轮工作已停止',
  } satisfies Record<RoomRoundRowState, string>)[state];
}

function compactText(value: string): string {
  const compact = value.replace(/\s+/gu, ' ').trim();
  return compact.length > 320 ? `${compact.slice(0, 317).trimEnd()}…` : compact;
}

function compactMarkdown(value: string): string {
  const compact = value.replace(/\r\n?/gu, '\n').trim();
  return compact.length > 320 ? `${compact.slice(0, 317).trimEnd()}…` : compact;
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function unique<T>(values: T[]): T[] {
  return [...new Set(values)];
}
