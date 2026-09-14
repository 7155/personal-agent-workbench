import type {
  RoomActivityProjection,
  RoomMessageProjection,
  RoomProjectionState,
} from '@/contracts/room-reducer';
import {
  roomActivityFlowKind,
  roomActivityHandoffKind,
  roomFlowRefs,
  roomWorkReviewFlow,
} from '@/features/rooms/room-flow-projection';
import { roomPlanetName } from '@/features/rooms/room-copy';
import type { RoomCollaborationRole, RoomSummary, RoomWorkItem, RoomWorkState } from '@/features/rooms/room-types';
import { selectPublicRoomTurnOrder } from '@/features/rooms/runtime/room-execution-lanes';
import {
  roomDispatchPlanFromActivity,
  roomDispatchPlans,
  roomDispatchPlanSummary,
  roomDispatchSourceParticipantId,
  type RoomDispatchPlan,
} from './room-gravity-projection';

export type RoomFocusState =
  | 'idle'
  | 'waiting'
  | 'running'
  | 'review'
  | 'blocked'
  | 'completed'
  | 'failed'
  | 'stopped'
  | 'disconnected';

export interface RoomFocusEvidence {
  ref: string;
  kind: 'artifact' | 'evidence';
}

export interface RoomFocusBlocker {
  reason: string;
  nextStep?: string;
}

/** Dual-axis independent review outcome: operability (does it run) and
 * requirement (does it satisfy the ask), plus which planet verified it. */
export interface RoomFocusReview {
  operability: string;
  requirement: string;
  reviewerParticipantId?: string;
  reason?: string;
}

/** Which parallel wave a WorkItem was dispatched inside, from route_decision. */
export interface RoomFocusWaveSlot {
  waveId: string;
  phaseName?: string;
  parallelIndex: number;
  parallelSize: number;
}

export interface RoomFocusWorkItem {
  id: string;
  parentId?: string;
  source: 'work-item' | 'runtime';
  objective: string;
  expectedOutput?: string;
  acceptanceCriteria: string[];
  ownerParticipantId?: string;
  offeredToParticipantId?: string;
  accountableParticipantId?: string;
  verifierParticipantId?: string;
  state: RoomFocusState;
  currentAction?: string;
  blocker?: RoomFocusBlocker;
  reviewRequired: boolean;
  review?: RoomFocusReview;
  wave?: RoomFocusWaveSlot;
  latestResult?: string;
  evidence: RoomFocusEvidence[];
  dispatchId?: string;
  updatedAtMs: number;
}

export interface RoomFocusPartner {
  participantId: string;
  sessionId: string;
  displayName: string;
  celestialName: string;
  ownerUserId?: string;
  ownerDisplayName?: string;
  collaborationRole?: RoomCollaborationRole;
  state: RoomFocusState;
  ownedWorkItemIds: string[];
  currentAction: string;
  latestReceipt?: string;
  unread: boolean;
}

export interface RoomFocusHandoff {
  id: string;
  sourceParticipantId: string;
  targetParticipantId: string;
  workItemId?: string;
  dispatchId?: string;
  artifactOrContract?: string;
  task?: string;
  state: 'offered' | 'dispatched' | 'completed' | 'failed' | 'stopped';
  createdAtMs: number;
}

export type RoomFocusPacketKind =
  | 'request'
  | 'intercom'
  | 'question'
  | 'answer'
  | 'plan'
  | 'document'
  | 'context'
  | 'result'
  | 'dispatch'
  | 'approval'
  | 'review';

/** One chronological "what moved between whom" entry: a real public message
 * or an authoritative transfer activity. `root` denotes Sol / the shared main
 * Room rather than a participant. WorkItems remain structural task-sheet data;
 * they do not fabricate traffic or gravity without a corresponding event. */
export interface RoomFocusPacket {
  id: string;
  sourceParticipantId: 'root' | string;
  targetParticipantIds: ('root' | string)[];
  kind: RoomFocusPacketKind;
  summary: string;
  status: string;
  createdAtMs: number;
  sequence: number;
  dispatchId?: string;
  workItemId?: string;
  /** Present on real routing decisions: how this assignment was made. */
  dispatchPlan?: RoomDispatchPlan;
  refs: string[];
  /** Durable direct-message identity; multiple delivery events are one message. */
  intercomId?: string;
  replyToPacketId?: string;
  receiptIds?: string[];
  deliveredAtMs?: number;
  repliedAtMs?: number;
  updatedAtMs?: number;
  error?: string;
  scope?: 'recent-room';
}

export interface RoomFocusProjection {
  goal: {
    title: string;
    description: string;
    rootId: string;
    state: RoomFocusState;
    rootResult?: RoomMessageProjection;
  };
  workItems: RoomFocusWorkItem[];
  partners: RoomFocusPartner[];
  handoffs: RoomFocusHandoff[];
  flow: RoomFocusPacket[];
  rootEvidence: RoomFocusEvidence[];
  counts: {
    active: number;
    review: number;
    blocked: number;
    completed: number;
  };
}

export function roomFocusCelestialName(ordinal: number): string {
  return roomPlanetName(ordinal);
}

export function buildRoomFocusProjection(
  room: RoomSummary,
  projection?: RoomProjectionState,
): RoomFocusProjection {
  const scope = currentRoomFocusScope(room, projection);
  const activities = scope.activities;
  const messages = scope.messages;
  const roomWorkItems = scope.workItems;
  const dispatchPlans = roomDispatchPlans(activities);
  const wavesByWorkItem = new Map<string, RoomFocusWaveSlot>();
  const wavesByDispatch = new Map<string, RoomFocusWaveSlot>();
  for (const plan of dispatchPlans) {
    if (!plan.waveId) continue;
    const slot: RoomFocusWaveSlot = {
      waveId: plan.waveId,
      ...(plan.phaseName ? { phaseName: plan.phaseName } : {}),
      parallelIndex: plan.parallelIndex,
      parallelSize: plan.parallelSize,
    };
    if (plan.workItemId) wavesByWorkItem.set(plan.workItemId, slot);
    if (plan.dispatchId) wavesByDispatch.set(plan.dispatchId, slot);
  }
  const orderedScopedWorkItems = orderedWorkItems(roomWorkItems);
  const explicit = orderedScopedWorkItems.map((item) => explicitFocusWork(item, activities, wavesByWorkItem));
  const rootByTurn = new Map<string, RoomFocusWorkItem>();
  for (const item of roomWorkItems) {
    if (item.parentWorkId) continue;
    const focus = explicit.find((candidate) => candidate.id === item.id);
    if (focus && item.rootTurnId) rootByTurn.set(item.rootTurnId, focus);
  }
  const defaultRoot = explicit.find((item) => !item.parentId);
  /* Dispatches whose route decision binds them to an explicit WorkItem are the
   * WorkItem — the tree must not show the same task twice. */
  const explicitIds = new Set(explicit.map((item) => item.id));
  const coveredDispatchIds = new Set(dispatchPlans
    .filter((plan) => plan.dispatchId && plan.workItemId && explicitIds.has(plan.workItemId))
    .map((plan) => plan.dispatchId));
  const runtime = runtimeFocusWork(activities, rootByTurn, defaultRoot, wavesByDispatch, explicitIds, coveredDispatchIds);
  const workItems = [...explicit, ...runtime];
  const partners = room.participants
    .slice()
    .sort((left, right) => left.ordinal - right.ordinal || left.id.localeCompare(right.id))
    .map((participant) => {
      const owned = workItems.filter((item) => [
        item.ownerParticipantId,
        item.offeredToParticipantId,
        item.accountableParticipantId,
      ].includes(participant.id));
      const latestActivity = [...activities].reverse().find((activity) => activity.participantId === participant.id);
      const latestMessage = [...messages].reverse().find((message) => message.participantId === participant.id && message.role === 'assistant');
      const state = participant.status === 'active'
        ? partnerFocusState(participant.id, owned, latestActivity, scope.turn)
        : 'disconnected';
      return {
        participantId: participant.id,
        sessionId: participant.sessionId,
        displayName: participant.displayName,
        celestialName: roomFocusCelestialName(participant.ordinal),
        ...(participant.ownerUserId ? { ownerUserId: participant.ownerUserId } : {}),
        ...(participant.ownerDisplayName?.trim() ? { ownerDisplayName: participant.ownerDisplayName.trim() } : {}),
        collaborationRole: participant.collaborationRole,
        state,
        ownedWorkItemIds: owned.map((item) => item.id),
        currentAction: stringValue(latestActivity?.payload.task)
          || latestActivity?.summary.trim()
          || owned.find((item) => ['running', 'review', 'blocked', 'waiting'].includes(item.state))?.objective
          || owned.at(0)?.objective
          || '等待新的工作项',
        latestReceipt: latestMessage?.text.trim()
          || owned.find((item) => item.latestResult)?.latestResult
          || (latestActivity?.status === 'completed' ? latestActivity.summary.trim() : undefined),
        unread: false,
      } satisfies RoomFocusPartner;
    });
  const handoffs = focusHandoffs(activities);
  const flow = focusFlowPackets(room, activities, messages, projection, dispatchPlans);
  const rootResult = [...messages].reverse().find((message) => (
    message.role === 'assistant'
    && message.status === 'completed'
    && ['result', 'work_result', 'review_result', 'handoff'].includes(message.postKind ?? 'result')
  ));
  const activeTopic = room.topics?.find((topic) => topic.id === room.activeTopicId)
    ?? room.topics?.find((topic) => topic.status === 'active');
  const rootWork = [...orderedScopedWorkItems].reverse().find((item) => !item.parentWorkId)
    ?? orderedScopedWorkItems.at(-1);
  const rootId = scope.turnId
    || rootWork?.rootTurnId
    || room.id;
  const turnState = scope.turn ? [turnStateValue(scope.turn.status)] : [];
  const goalState = strongestState([
    ...workItems.map((item) => item.state),
    ...turnState,
  ]);
  const rootEvidence = uniqueEvidence(roomWorkItems.flatMap((item) => [
    ...item.artifactRefs.map((ref) => ({ ref, kind: 'artifact' as const })),
    ...item.evidenceRefs.map((ref) => ({ ref, kind: 'evidence' as const })),
  ]));

  return {
    goal: {
      title: activeTopic?.title.trim() || rootWork?.objective.trim() || room.title,
      description: activeTopic?.summary.trim() || room.description?.trim() || rootWork?.expectedOutput.trim() || '',
      rootId,
      state: goalState,
      ...(rootResult ? { rootResult } : {}),
    },
    workItems,
    partners,
    handoffs,
    flow,
    rootEvidence,
    counts: {
      active: workItems.filter((item) => item.state === 'running' || item.state === 'waiting').length,
      review: workItems.filter((item) => item.state === 'review').length,
      blocked: workItems.filter((item) => item.state === 'blocked' || item.state === 'failed').length,
      completed: workItems.filter((item) => item.state === 'completed').length,
    },
  };
}

/**
 * Room focus is a live projection, not the history browser. Once a public Root
 * exists, every current-status input must come from its latest attempt only.
 * Older rounds remain available in their own task sheets but cannot leave the
 * Room header or a planet permanently failed/blocked (UR-162/173).
 */
function currentRoomFocusScope(
  room: RoomSummary,
  projection?: RoomProjectionState,
): {
  activities: RoomActivityProjection[];
  messages: RoomMessageProjection[];
  workItems: RoomWorkItem[];
  turnId?: string;
  turn?: RoomProjectionState['turnsById'][string];
} {
  const activities = orderedActivities(projection);
  const messages = orderedMessages(projection);
  if (!projection) return { activities, messages, workItems: room.workItems ?? [] };

  const turnId = selectPublicRoomTurnOrder(projection).at(-1);
  const turn = turnId ? projection.turnsById[turnId] : undefined;
  if (!turnId || !turn) return { activities, messages, workItems: room.workItems ?? [] };

  const activityIds = new Set(turn.activityIds);
  const messageIds = new Set(turn.messageIds);
  const scopedWorkItems = (room.workItems ?? []).filter((item) => item.rootTurnId === turnId);
  return {
    activities: activities.filter((activity) => activityIds.has(activity.id)),
    messages: messages.filter((message) => messageIds.has(message.id)),
    /* A metadata refresh can publish the latest public turn before its
       WorkItem's rootTurnId is attached to the Room snapshot. Keep the
       authoritative roster visible during that short skew; otherwise the
       collaboration graph silently loses each planet's actual assignment. */
    workItems: scopedWorkItems.length ? scopedWorkItems : room.workItems ?? [],
    turnId,
    turn,
  };
}

/**
 * Sol is the coordinator's chair, not a decoration. Until a real participant
 * is still connected and actually holds `collaborationRole === 'coordinator'`,
 * no surface may draw a Sol origin, mission header or centre body — an
 * unhosted Room has no centre to orbit (Joshua5: 没有主持时不要画 Sol 原点).
 */
export function roomFocusHasCoordinator(
  partners: readonly { collaborationRole?: string; state: RoomFocusState }[],
): boolean {
  return partners.some((partner) => (
    partner.collaborationRole === 'coordinator' && partner.state !== 'disconnected'
  ));
}

/** What the shared origin is called wherever a packet or fallback names it. */
export function roomFocusOriginLabel(hasCoordinator: boolean): string {
  return hasCoordinator ? 'Sol' : '主 Room';
}

export function roomFocusStateLabel(state: RoomFocusState): string {
  return ({
    idle: '待命',
    waiting: '等待',
    running: '进行中',
    review: '等待复核',
    blocked: '阻塞',
    completed: '已完成',
    failed: '需要关注',
    stopped: '已停止',
    disconnected: '已离线',
  } satisfies Record<RoomFocusState, string>)[state];
}

function explicitFocusWork(
  item: RoomWorkItem,
  activities: RoomActivityProjection[],
  wavesByWorkItem?: Map<string, RoomFocusWaveSlot>,
): RoomFocusWorkItem {
  const ownerId = item.currentOwnerParticipantId || item.offeredToParticipantId || item.accountableParticipantId || undefined;
  const latestActivity = [...activities].reverse().find((activity) => (
    activity.turnId === item.rootTurnId
    && (!ownerId || activity.participantId === ownerId)
  ));
  const blocker = focusBlocker(item.blocker);
  const review = focusReview(item);
  const wave = wavesByWorkItem?.get(item.id);
  return {
    id: item.id,
    ...(item.parentWorkId ? { parentId: item.parentWorkId } : {}),
    source: 'work-item',
    objective: item.objective,
    expectedOutput: item.expectedOutput || undefined,
    acceptanceCriteria: item.acceptanceCriteria,
    ownerParticipantId: ownerId,
    offeredToParticipantId: item.offeredToParticipantId || undefined,
    accountableParticipantId: item.accountableParticipantId || undefined,
    verifierParticipantId: review?.reviewerParticipantId,
    state: workState(item.state),
    currentAction: latestActivity?.summary.trim() || undefined,
    blocker,
    reviewRequired: item.state === 'review',
    ...(review ? { review } : {}),
    ...(wave ? { wave } : {}),
    latestResult: item.resultSummary.trim() || undefined,
    evidence: uniqueEvidence([
      ...item.artifactRefs.map((ref) => ({ ref, kind: 'artifact' as const })),
      ...item.evidenceRefs.map((ref) => ({ ref, kind: 'evidence' as const })),
    ]),
    updatedAtMs: item.updatedAtMs,
  };
}

/** Only a real recorded verdict becomes a review row; empty strings stay out. */
function focusReview(item: RoomWorkItem): RoomFocusReview | undefined {
  const review = item.review;
  if (!review) return undefined;
  const operability = review.operabilityVerdict.trim();
  const requirement = review.requirementVerdict.trim();
  if (!operability && !requirement) return undefined;
  return {
    operability,
    requirement,
    ...(review.reviewerParticipantId ? { reviewerParticipantId: review.reviewerParticipantId } : {}),
    ...(review.reason.trim() ? { reason: review.reason.trim() } : {}),
  };
}

/** Runtime rows only surface dispatched work that has no explicit WorkItem
 * (e.g. private tool agents). A dispatch already bound to a WorkItem stays a
 * single row — the WorkItem itself carries owner, wave and review. */
function runtimeFocusWork(
  activities: RoomActivityProjection[],
  rootByTurn: Map<string, RoomFocusWorkItem>,
  defaultRoot?: RoomFocusWorkItem,
  wavesByDispatch?: Map<string, RoomFocusWaveSlot>,
  explicitIds?: Set<string>,
  coveredDispatchIds?: Set<string>,
): RoomFocusWorkItem[] {
  const byDispatch = new Map<string, RoomFocusWorkItem>();
  for (const activity of activities) {
    const task = stringValue(activity.payload.task);
    const dispatchId = stringValue(activity.payload.dispatchId || activity.payload.childDispatchId);
    if (!task || !dispatchId) continue;
    if (coveredDispatchIds?.has(dispatchId)) continue;
    const boundWorkId = stringValue(activity.payload.workItemId);
    if (boundWorkId && explicitIds?.has(boundWorkId)) continue;
    const previous = byDispatch.get(dispatchId);
    const parent = rootByTurn.get(activity.turnId) ?? defaultRoot;
    const owner = activity.participantId
      || stringValue(activity.payload.targetParticipantId)
      || undefined;
    const wave = wavesByDispatch?.get(dispatchId);
    byDispatch.set(dispatchId, {
      id: `runtime:${dispatchId}`,
      ...(parent ? { parentId: parent.id } : {}),
      source: 'runtime',
      objective: task,
      expectedOutput: stringValue(activity.payload.expectedOutput) || previous?.expectedOutput,
      acceptanceCriteria: stringArray(activity.payload.acceptanceCriteria).length
        ? stringArray(activity.payload.acceptanceCriteria)
        : previous?.acceptanceCriteria ?? [],
      ownerParticipantId: owner ?? previous?.ownerParticipantId,
      state: activityState(activity.status),
      currentAction: activity.summary.trim() || previous?.currentAction,
      reviewRequired: stringValue(activity.payload.requestKind) === 'plan_review' || activity.status === 'waiting',
      ...(wave ? { wave } : previous?.wave ? { wave: previous.wave } : {}),
      latestResult: activity.status === 'completed' ? activity.summary.trim() : previous?.latestResult,
      evidence: previous?.evidence ?? [],
      dispatchId,
      updatedAtMs: activity.updatedAtMs ?? activity.createdAtMs,
    });
  }
  return [...byDispatch.values()].sort((left, right) => left.updatedAtMs - right.updatedAtMs || left.id.localeCompare(right.id));
}

function focusHandoffs(activities: RoomActivityProjection[]): RoomFocusHandoff[] {
  const rows: RoomFocusHandoff[] = [];
  for (const activity of activities) {
    if (roomActivityHandoffKind(activity) !== 'handoff') continue;
    const work = recordValue(activity.payload.work);
    const target = stringValue(activity.payload.targetParticipantId)
      || stringValue(activity.payload.currentOwnerParticipantId)
      || stringValue(work.offeredToParticipantId)
      || stringValue(work.currentOwnerParticipantId);
    if (!target) continue;
    const source = stringValue(activity.payload.sourceParticipantId || activity.payload.previousOwnerParticipantId || activity.payload.parentParticipantId)
      || stringValue(activity.participantId);
    if (!source || source === target) continue;
    const dispatchId = stringValue(activity.payload.dispatchId || activity.payload.childDispatchId);
    const workItemId = stringValue(activity.payload.workItemId) || stringValue(work.id);
    rows.push({
      id: `activity:${activity.id}`,
      sourceParticipantId: source,
      targetParticipantId: target,
      workItemId: workItemId || undefined,
      dispatchId: dispatchId || undefined,
      artifactOrContract: stringValue(activity.payload.expectedOutput) || stringValue(work.expectedOutput) || undefined,
      task: stringValue(activity.payload.task) || stringValue(work.objective) || activity.summary.trim() || undefined,
      state: activity.status === 'failed'
        ? 'failed'
        : activity.status === 'aborted'
          ? 'stopped'
          : activity.status === 'completed'
            ? 'completed'
            : 'dispatched',
      createdAtMs: activity.createdAtMs,
    });
  }
  const byIdentity = new Map<string, RoomFocusHandoff>();
  for (const row of rows) byIdentity.set(`${row.sourceParticipantId}:${row.targetParticipantId}:${row.workItemId ?? row.dispatchId ?? row.id}`, row);
  return [...byIdentity.values()].sort((left, right) => left.createdAtMs - right.createdAtMs || left.id.localeCompare(right.id));
}

/** Chronological flow ledger. Only authoritative transfers become packets —
 * a public message, an approval, a dispatch/route decision, an intercom
 * request, or refs explicitly handed to a named target (UR-054/UR-057). Refs
 * on a partner's own tool activity never fabricate a decorative packet. */
function focusFlowPackets(
  room: RoomSummary,
  activities: RoomActivityProjection[],
  messages: RoomMessageProjection[],
  projection?: RoomProjectionState,
  dispatchPlans: RoomDispatchPlan[] = [],
): RoomFocusPacket[] {
  const packets: RoomFocusPacket[] = [];
  const celestialByParticipant = new Map(room.participants.map((participant) => (
    [participant.id, roomFocusCelestialName(participant.ordinal)]
  )));
  for (const activity of activities) {
    // The reducer groups intercom activities under kind=dispatch for legacy
    // activity presentation; the producer's activityKind is more specific.
    const kind = stringValue(activity.payload.activityKind) === 'intercom' ? 'intercom' : roomActivityFlowKind(activity);
    if (!kind) continue;
    /* Production intercom events keep the endpoints/content inside message.
       Never turn a real partner-to-partner delivery into root -> recipient. */
    const direct = kind === 'intercom' ? recordValue(activity.payload.message) : {};
    if (stringValue(direct.id) && stringValue(direct.sourceParticipantId) && stringValue(direct.targetParticipantId)) {
      packets.push({
        id: `intercom:${stringValue(direct.id)}`,
        intercomId: stringValue(direct.id),
        sourceParticipantId: stringValue(direct.sourceParticipantId),
        targetParticipantIds: [stringValue(direct.targetParticipantId)],
        kind: direct.kind === 'ask' ? 'question' : direct.kind === 'reply' ? 'answer' : 'intercom',
        summary: stringValue(direct.content) || activity.summary,
        status: stringValue(direct.status) || stringValue(activity.payload.phase) || activity.status,
        createdAtMs: activity.createdAtMs,
        updatedAtMs: activity.updatedAtMs ?? activity.createdAtMs,
        sequence: activity.sequence ?? activity.createdAtMs,
        ...(stringValue(direct.replyTo) ? { replyToPacketId: `intercom:${stringValue(direct.replyTo)}` } : {}),
        ...(stringValue(direct.workItemId) ? { workItemId: stringValue(direct.workItemId) } : {}),
        ...(stringValue(direct.error) ? { error: stringValue(direct.error) } : {}),
        receiptIds: [activity.id],
        refs: roomFlowRefs(activity.payload),
      });
      continue;
    }
    const review = kind === 'review' ? roomWorkReviewFlow(activity) : undefined;
    const plan = kind === 'dispatch' ? roomDispatchPlanFromActivity(activity) : undefined;
    const isApproval = kind === 'approval';
    const isReview = kind === 'review';
    const targetId = isApproval
      ? 'root'
      : isReview
        ? review?.targetParticipantId || reviewTargetParticipantId(activity)
        : plan?.targetParticipantId || stringValue(activity.payload.targetParticipantId) || activity.participantId || '';
    if (!targetId) continue;
    /* A child dispatch is exerted by the target of its parent dispatch; a
     * root dispatch is Sol's own gravity. */
    const planSource = plan ? roomDispatchSourceParticipantId(plan, dispatchPlans) : '';
    const planSummary = plan
      ? roomDispatchPlanSummary(plan, celestialByParticipant.get(plan.targetParticipantId) ?? '协作行星')
      : '';
    packets.push({
      id: `activity:${activity.id}`,
      sourceParticipantId: review?.sourceParticipantId
        || stringValue(activity.payload.sourceParticipantId)
        || stringValue(activity.payload.actorParticipantId)
        || stringValue(activity.payload.parentParticipantId)
        || planSource
        || (isApproval ? activity.participantId ?? '' : '')
        || (isReview ? stringValue(activity.payload.reviewerParticipantId) : '')
        || 'root',
      targetParticipantIds: [targetId],
      kind,
      summary: planSummary || activity.summary.trim() || stringValue(activity.payload.reason) || '已确认本轮分工',
      status: review?.status ?? activity.status,
      createdAtMs: activity.createdAtMs,
      sequence: activity.sequence ?? activity.createdAtMs,
      dispatchId: stringValue(activity.payload.dispatchId) || undefined,
      workItemId: review?.workItemId
        || stringValue(activity.payload.workItemId)
        || stringValue(activity.payload.taskId)
        || undefined,
      ...(plan ? { dispatchPlan: plan } : {}),
      refs: review?.refs ?? roomFlowRefs(activity.payload),
    });
  }
  for (const message of messages) {
    if (message.projectionKind === 'execution' || !message.text.trim()) continue;
    const packet = packetFromMessage(message, projection);
    if (packet) packets.push(packet);
  }
  const unique = new Map<string, RoomFocusPacket>();
  for (const packet of packets.sort((left, right) => left.sequence - right.sequence || left.createdAtMs - right.createdAtMs)) {
    const previous = unique.get(packet.id);
    unique.set(packet.id, previous?.intercomId ? {
      ...packet,
      createdAtMs: previous.createdAtMs,
      sequence: previous.sequence,
      receiptIds: [...new Set([...(previous.receiptIds ?? []), ...(packet.receiptIds ?? [])])],
    } : packet);
  }
  return [...unique.values()];
}

/** A review relation must name its counterpart in a bounded event field. Do
 * not infer the target from prose, role labels, or a WorkItem. */
function reviewTargetParticipantId(activity: RoomActivityProjection): string {
  return stringValue(activity.payload.targetParticipantId)
    || stringValue(activity.payload.reviewerParticipantId)
    || stringValue(activity.payload.verifierParticipantId)
    || stringValue(activity.payload.reviewedParticipantId)
    || stringValue(activity.payload.revieweeParticipantId);
}

function packetFromMessage(
  message: RoomMessageProjection,
  projection?: RoomProjectionState,
): RoomFocusPacket | undefined {
  const sourceParticipantId = message.role === 'user' ? 'root' : message.participantId || 'root';
  let targetParticipantIds: string[] = [...(message.mentionedParticipantIds ?? [])];
  let kind: RoomFocusPacketKind = message.role === 'user' ? 'request' : 'result';
  if (message.answerToPostId) {
    const question = projection?.messagesById[message.answerToPostId];
    targetParticipantIds = question?.participantId ? [question.participantId] : [];
    kind = 'answer';
  } else if (message.question) {
    targetParticipantIds = ['root'];
    kind = 'question';
  } else if (message.role === 'assistant' && targetParticipantIds.length === 0) {
    targetParticipantIds = ['root'];
  }
  const blockKinds = message.message?.blocks.map((block) => block.type) ?? [];
  if (blockKinds.includes('task_plan') || message.postKind === 'plan') kind = 'plan';
  else if (blockKinds.some((blockKind) => ['file', 'artifact', 'diff'].includes(blockKind))) kind = 'document';
  if (sourceParticipantId === 'root' && targetParticipantIds.length === 0) return undefined;
  return {
    id: `message:${message.id}`,
    sourceParticipantId,
    targetParticipantIds,
    kind,
    summary: message.text,
    status: message.status,
    createdAtMs: message.createdAtMs,
    sequence: message.sequence ?? message.createdAtMs,
    dispatchId: message.dispatchId,
    ...(message.answerToPostId ? { replyToPacketId: `message:${message.answerToPostId}` } : {}),
    refs: messagePacketRefs(message),
  };
}

function messagePacketRefs(message: RoomMessageProjection): string[] {
  const refs = message.message?.blocks.flatMap((block) => {
    const data = recordValue(block.data);
    return [
      stringValue(block.ref),
      stringValue(data.ref),
      stringValue(data.path),
      stringValue(data.documentId),
      stringValue(data.revision),
    ].filter(Boolean);
  }) ?? [];
  return [...new Set(refs)];
}

function orderedWorkItems(items: RoomWorkItem[]): RoomWorkItem[] {
  const byParent = new Map<string, RoomWorkItem[]>();
  for (const item of items) {
    const parent = item.parentWorkId || '';
    const group = byParent.get(parent) ?? [];
    group.push(item);
    byParent.set(parent, group);
  }
  for (const group of byParent.values()) group.sort((left, right) => left.createdAtMs - right.createdAtMs || left.id.localeCompare(right.id));
  const result: RoomWorkItem[] = [];
  const visit = (item: RoomWorkItem) => {
    if (result.some((candidate) => candidate.id === item.id)) return;
    result.push(item);
    for (const child of byParent.get(item.id) ?? []) visit(child);
  };
  for (const root of byParent.get('') ?? []) visit(root);
  for (const item of items) visit(item);
  return result;
}

function orderedActivities(projection?: RoomProjectionState): RoomActivityProjection[] {
  if (!projection) return [];
  return projection.activityOrder
    .map((id) => projection.activitiesById[id])
    .filter((item): item is RoomActivityProjection => Boolean(item))
    .sort((left, right) => (left.sequence ?? left.createdAtMs) - (right.sequence ?? right.createdAtMs) || left.id.localeCompare(right.id));
}

function orderedMessages(projection?: RoomProjectionState): RoomMessageProjection[] {
  if (!projection) return [];
  return projection.messageOrder
    .map((id) => projection.messagesById[id])
    .filter((item): item is RoomMessageProjection => Boolean(item))
    .sort((left, right) => (left.sequence ?? left.createdAtMs) - (right.sequence ?? right.createdAtMs) || left.id.localeCompare(right.id));
}

function workState(state: RoomWorkState): RoomFocusState {
  return ({
    queued: 'waiting',
    active: 'running',
    review: 'review',
    blocked: 'blocked',
    done: 'completed',
    failed: 'failed',
    cancelled: 'stopped',
  } satisfies Record<RoomWorkState, RoomFocusState>)[state];
}

function activityState(status: RoomActivityProjection['status']): RoomFocusState {
  return ({
    running: 'running',
    waiting: 'review',
    completed: 'completed',
    failed: 'failed',
    aborted: 'stopped',
  } satisfies Record<RoomActivityProjection['status'], RoomFocusState>)[status];
}

/**
 * A red tool receipt is not, by itself, a participant verdict. The current
 * Room turn owns the live/completed lifecycle; explicit participant and
 * WorkItem failures still win, while the failed receipt remains available to
 * the activity/Trace surfaces through `latestActivity` and the scoped flow.
 */
function partnerFocusState(
  participantId: string,
  owned: RoomFocusWorkItem[],
  latestActivity: RoomActivityProjection | undefined,
  turn: RoomProjectionState['turnsById'][string] | undefined,
): RoomFocusState {
  const participates = Boolean(turn && (
    turn.participantIds.includes(participantId)
    || turn.failedParticipantIds?.includes(participantId)
    || turn.abortedParticipantIds?.includes(participantId)
    || turn.terminalParticipantIds?.includes(participantId)
    || latestActivity
  ));
  const authoritativeState = turn && participates
    ? participantTurnState(turn, participantId)
    : undefined;
  const recoverableActivityFailure = latestActivity?.status === 'failed'
    && (authoritativeState === 'running' || authoritativeState === 'completed');

  /* A participant terminal is execution authority. Room metadata can lag one
   * refresh behind and still call its WorkItem active; that historical task
   * state must not turn a submitted partner result back into "进行中". The
   * Root turn remains independent and may continue integrating other lanes. */
  if (authoritativeState && ['completed', 'failed', 'stopped'].includes(authoritativeState)) {
    return authoritativeState;
  }

  return strongestState([
    ...owned.map((item) => item.state),
    ...(authoritativeState ? [authoritativeState] : []),
    ...(!recoverableActivityFailure && latestActivity ? [activityState(latestActivity.status)] : []),
  ]);
}

function participantTurnState(
  turn: RoomProjectionState['turnsById'][string],
  participantId: string,
): RoomFocusState {
  if (turn.failedParticipantIds?.includes(participantId)) return 'failed';
  if (turn.abortedParticipantIds?.includes(participantId)) return 'stopped';
  if (turn.terminalParticipantIds?.includes(participantId)) return 'completed';
  return turnStateValue(turn.status);
}

function turnStateValue(status: 'queued' | 'running' | 'completed' | 'failed' | 'aborted'): RoomFocusState {
  return ({ queued: 'waiting', running: 'running', completed: 'completed', failed: 'failed', aborted: 'stopped' })[status] as RoomFocusState;
}

function strongestState(states: RoomFocusState[]): RoomFocusState {
  const priority: RoomFocusState[] = ['blocked', 'failed', 'review', 'running', 'waiting', 'disconnected', 'completed', 'stopped', 'idle'];
  return priority.find((state) => states.includes(state)) ?? 'idle';
}

function focusBlocker(value: Record<string, unknown>): RoomFocusBlocker | undefined {
  const reason = stringValue(value.reason);
  if (!reason) return undefined;
  const nextStep = stringValue(value.nextStep);
  return { reason, ...(nextStep ? { nextStep } : {}) };
}

function uniqueEvidence(items: RoomFocusEvidence[]): RoomFocusEvidence[] {
  const byRef = new Map<string, RoomFocusEvidence>();
  for (const item of items) if (item.ref.trim()) byRef.set(item.ref, item);
  return [...byRef.values()];
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function recordValue(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && Boolean(item.trim())) : [];
}
