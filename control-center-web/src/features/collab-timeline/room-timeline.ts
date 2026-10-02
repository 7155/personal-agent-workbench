import type { RoomActivityProjection, RoomMessageProjection, RoomProjectionState } from '@/contracts/room-reducer';
import type { AgentSubagentRunV1 } from '@/contracts/generated/agent-subagent-run.v1';
import { roomPlanetName } from '@/features/rooms/room-copy';
import type { RoomCollaborationRole, RoomParticipant, RoomSummary } from '@/features/rooms/room-types';
import { selectPublicRoomTurnOrder } from '@/features/rooms/runtime/room-execution-lanes';
import { JEV_TASK_STAGE_LABELS, jevTaskStage, type JevSnapshot } from '@/features/semantic-workspace/jev-execution';
import {
  collabPhasesFromEvidence,
  type CollabEvent,
  type CollabHandoff,
  type CollabLane,
  type CollabLaneState,
  type CollabMark,
  type CollabSegment,
  type CollabSegmentKind,
  type CollabTimeline,
  type CollabTask,
} from './model';

/**
 * Room → timeline. The real workflow this reads:
 *
 * - The user message opens a Root in the shared Room (the origin lane).
 * - `route_decision` starts a partner dispatch. The coordinator planet
 *   (collaborationRole=coordinator, "行星协调") owns the team: it recruits
 *   partners (`room_partner op=add_participant` → participant_joined),
 *   delegates (`delegate` / `delegate_batch` → child route_decision whose
 *   parentDispatchId names its own dispatch), collects and accepts/returns
 *   WorkItems, and posts the final result.
 * - Jev, when enabled, is only a helper: its dispatches carry
 *   routingPolicy=jev, a purpose (plan/execute/verify/synthesize) and the
 *   model it selected. It never becomes a lane.
 * - Tool Agent satellites are temporary `agents` runs under one partner's
 *   Session. They are drawn as thin sub-lanes of that planet.
 */

export interface RoomTimelineInput {
  room: RoomSummary;
  projection?: RoomProjectionState;
  /** Optional Jev snapshot: supplies purpose/model labels, never lanes. */
  graph?: JevSnapshot | null;
  /** Retained Tool Agent runs per participant Session, when already loaded. */
  satellites?: Record<string, readonly AgentSubagentRunV1[] | undefined>;
  nowMs?: number;
  live?: boolean;
  /** Pick a specific root; defaults to the latest public one. */
  rootId?: string;
}

export const ORIGIN_LANE = 'room';

const ROLE_LABELS: Record<RoomCollaborationRole, string> = {
  coordinator: '行星协调',
  researcher: '调研伙伴',
  implementer: '执行伙伴',
  reviewer: '复核伙伴',
  specialist: '专项伙伴',
};
const PURPOSE_KIND: Record<string, CollabSegmentKind> = { plan: 'plan', execute: 'execute', verify: 'review', synthesize: 'synthesize' };
const PURPOSE_LABEL: Record<string, string> = { plan: '规划分工', execute: '执行任务', verify: '结果复核', synthesize: '汇总答复' };

interface Dispatch {
  id: string;
  laneId: string;
  parentId: string;
  sourceLaneId: string;
  purpose: string;
  label: string;
  startMs: number;
  endMs?: number;
  failed: boolean;
  aborted: boolean;
  model: string;
  workItemId: string;
}

export function buildRoomCollabTimeline(input: RoomTimelineInput): CollabTimeline {
  const { room, projection } = input;
  const nowMs = input.nowMs ?? Date.now();
  const rootId = input.rootId ?? (projection ? selectPublicRoomTurnOrder(projection).at(-1) : undefined) ?? '';
  const graph = input.graph?.rootId === rootId ? input.graph : undefined;
  const turn = rootId && projection ? projection.turnsById[rootId] : undefined;
  const activities = projection && turn
    ? turn.activityIds.map((id) => projection.activitiesById[id]).filter((item): item is RoomActivityProjection => Boolean(item))
    : [];
  const messages = projection && turn
    ? turn.messageIds.map((id) => projection.messagesById[id]).filter((item): item is RoomMessageProjection => Boolean(item))
    : [];
  activities.sort(byTime);
  messages.sort(byTime);
  const live = input.live ?? (turn ? turn.status === 'running' || turn.status === 'queued' : false);
  const stopped = turn?.status === 'aborted' || Boolean(graph?.stopped);
  const stopping = stopped && (live || Boolean(graph?.effects.some(effect => ['running', 'unknown'].includes(effect.executionStatus ?? ''))));
  const participants = [...room.participants].sort((a, b) => a.ordinal - b.ordinal);
  const participantById = new Map(participants.map((participant) => [participant.id, participant]));
  const laneOf = (participantId: string | null | undefined) => (participantId && participantById.has(participantId) ? participantId : ORIGIN_LANE);
  const nameOf = (laneId: string) => (laneId === ORIGIN_LANE ? '团队调度' : roomPlanetName(participantById.get(laneId)?.ordinal ?? 0));

  const segments: CollabSegment[] = [];
  const handoffs: CollabHandoff[] = [];
  const marks: CollabMark[] = [];
  const events: CollabEvent[] = [];
  const reached = new Set<string>();
  const phaseFailures = new Set<string>();
  const joined = new Map<string, number>();
  const involved = new Set<string>();
  const models = new Map<string, string>();
  const dispatches = new Map<string, Dispatch>();

  const startMs = Math.min(
    turn?.createdAtMs ?? Number.POSITIVE_INFINITY,
    ...activities.map((item) => item.createdAtMs),
    ...messages.map((item) => item.createdAtMs),
  );
  const firstMs = Number.isFinite(startMs) ? startMs : nowMs;

  // Jev effects give purpose/model for a dispatch id (Jev is a helper only).
  const jevEffects = new Map<string, { purpose: string; model: string; taskId: string; objective: string }>();
  for (const effect of graph?.effects ?? []) {
    if (effect.operation !== 'dispatch') continue;
    const selection = record(record(record(effect.request.contextManifest).executionScope).modelSelection);
    jevEffects.set(effect.effectId, {
      purpose: text(effect.request.purpose) || 'execute',
      taskId: text(effect.request.taskId),
      objective: text(effect.request.objective),
      model: [text(selection.modelId), text(selection.thinkingLevel)].filter(Boolean).join(' · '),
    });
  }
  if (graph?.planApproval || graph?.phase === 'plan') reached.add('plan');
  for (const effect of graph?.effects ?? []) {
    if (effect.operation !== 'dispatch') continue;
    const purpose = text(effect.request.purpose);
    if (purpose === 'plan') reached.add('plan');
    if (purpose === 'execute') reached.add('execute');
    if (purpose === 'verify') reached.add('review');
    if (purpose === 'synthesize') reached.add('reply');
  }

  // 1. The request.
  const userMessage = messages.find((message) => message.role === 'user');
  if (userMessage) {
    events.push({ id: `u:${userMessage.id}`, laneId: ORIGIN_LANE, atMs: userMessage.createdAtMs, tone: 'origin', actor: '你', text: `发出需求「${short(userMessage.text, 40)}」` });
  }

  // 2. Dispatches — every route_decision opens a responsibility window.
  for (const activity of activities) {
    if (activity.kind !== 'route_decision') continue;
    const payload = activity.payload;
    const dispatchId = text(payload.dispatchId) || activity.id;
    const laneId = laneOf(text(payload.targetParticipantId) || activity.participantId);
    if (laneId === ORIGIN_LANE) continue;
    const parentId = text(payload.parentDispatchId);
    const jev = jevEffects.get(dispatchId);
    const purpose = text(payload.purpose) || jev?.purpose || (payload.reason === 'partner_delegate' ? 'execute' : '');
    const parent = parentId ? dispatches.get(parentId) : undefined;
    const sourceLaneId = parent?.laneId
      ?? (text(payload.sourceParticipantId) ? laneOf(text(payload.sourceParticipantId)) : ORIGIN_LANE);
    const participant = participantById.get(laneId);
    const role = participant?.collaborationRole;
    const label = PURPOSE_LABEL[purpose]
      ?? (role === 'coordinator' && !parentId ? '协调本轮' : '执行任务');
    const model = jev?.model ?? '';
    if (model) models.set(laneId, model);
    dispatches.set(dispatchId, { id: dispatchId, laneId, parentId, sourceLaneId, purpose, label, startMs: activity.createdAtMs, failed: false, aborted: false, model, workItemId: text(payload.workItemId) || jev?.taskId || '' });
    involved.add(laneId);
    handoffs.push({
      id: `d:${dispatchId}`,
      kind: 'dispatch',
      fromLaneId: sourceLaneId,
      toLaneId: laneId,
      atMs: activity.createdAtMs,
      label: payload.reason === 'partner_delegate' ? '委派' : purpose === 'verify' ? '复核分派' : purpose === 'plan' ? '规划分派' : purpose === 'synthesize' ? '汇总指令' : '分派',
    });
    if (model) marks.push({ id: `m:${dispatchId}`, laneId, kind: 'model', atMs: activity.createdAtMs, label: `Jev 选择 ${model}` });
    events.push({
      id: `d:${dispatchId}`, laneId, atMs: activity.createdAtMs,
      tone: purpose === 'verify' ? 'review' : purpose === 'plan' || purpose === 'synthesize' ? 'plan' : 'execute',
      actor: `${nameOf(sourceLaneId)} → ${nameOf(laneId)}`,
      text: `${label}${model ? ` · Jev 选择 ${model}` : ''}`,
    });
  }

  // 3. Lifecycle receipts: child terminals, WorkItem review, intercom, recruiting, tools.
  const toolCalls = new Set<string>();
  let failedTools = 0;
  for (const activity of activities) {
    const payload = activity.payload;
    const activityKind = text(payload.activityKind);
    const phase = text(payload.phase);
    const laneId = laneOf(activity.participantId);
    if (activity.kind === 'route_decision') continue;

    if (activityKind === 'child' && ['completed', 'failed', 'aborted'].includes(phase)) {
      const dispatch = dispatches.get(text(payload.childDispatchId) || text(payload.dispatchId));
      if (dispatch && dispatch.endMs === undefined) {
        dispatch.endMs = activity.createdAtMs;
        dispatch.failed = phase === 'failed';
        dispatch.aborted = phase === 'aborted';
        if (dispatch.failed) {
          const kind = PURPOSE_KIND[dispatch.purpose];
          if (kind === 'plan' || kind === 'execute' || kind === 'review') phaseFailures.add(kind);
          if (kind === 'synthesize') phaseFailures.add('reply');
        }
        handoffs.push({ id: `r:${dispatch.id}`, kind: 'submit', fromLaneId: dispatch.laneId, toLaneId: dispatch.sourceLaneId, atMs: activity.createdAtMs, label: phase === 'completed' ? '交回结果' : phase === 'aborted' ? '已停止' : '执行失败', failed: phase !== 'completed' });
        events.push({ id: `c:${activity.id}`, laneId: dispatch.laneId, atMs: activity.createdAtMs, tone: phase === 'completed' ? 'done' : 'fail', actor: nameOf(dispatch.laneId), text: phase === 'completed' ? `交回结果${activity.summary && !isMachine(activity.summary) ? `：${short(activity.summary, 48)}` : ''}` : phase === 'aborted' ? '执行已停止' : '执行失败' });
      }
      continue;
    }

    if (activityKind === 'work') {
      const work = record(payload.work);
      const actor = laneOf(activity.participantId);
      if (phase === 'submitted') {
        const target = laneOf(text(work.createdByParticipantId) || text(work.accountableParticipantId));
        handoffs.push({ id: `ws:${activity.id}`, kind: 'submit', fromLaneId: actor, toLaneId: target, atMs: activity.createdAtMs, label: '提交结果', pending: true });
        events.push({ id: `ws:${activity.id}`, laneId: actor, atMs: activity.createdAtMs, tone: 'wait', actor: nameOf(actor), text: `提交结果，等待 ${nameOf(target)} 验收` });
        // Submission is awaiting review, not proof that a reviewer ran.
      } else if (phase === 'completed') {
        marks.push({ id: `wa:${activity.id}`, laneId: actor, kind: 'accept', atMs: activity.createdAtMs, label: '已验收' });
        events.push({ id: `wa:${activity.id}`, laneId: actor, atMs: activity.createdAtMs, tone: 'done', actor: nameOf(actor), text: `验收通过：${short(text(work.objective), 40)}` });
        /* Jev task states are the acceptance authority when a graph is
         * present. A Room work receipt alone can describe a partial handoff
         * and must not create a green acceptance phase for a failed graph. */
        if (!graph) reached.add('accept');
      } else if (phase === 'returned') {
        phaseFailures.add('accept');
        const target = laneOf(text(work.currentOwnerParticipantId));
        marks.push({ id: `wr:${activity.id}`, laneId: actor, kind: 'return', atMs: activity.createdAtMs, label: '退回返修' });
        handoffs.push({ id: `wr:${activity.id}`, kind: 'return', fromLaneId: actor, toLaneId: target, atMs: activity.createdAtMs, label: '退回返修', failed: false });
        events.push({ id: `wr:${activity.id}`, laneId: actor, atMs: activity.createdAtMs, tone: 'fail', actor: nameOf(actor), text: `退回 ${nameOf(target)} 返修` });
      } else if (phase === 'assigned') {
        involved.add(laneOf(text(work.currentOwnerParticipantId)));
      }
      continue;
    }

    if (activityKind === 'intercom') {
      const message = record(payload.message);
      const from = laneOf(text(message.sourceParticipantId));
      const to = laneOf(text(message.targetParticipantId));
      if (from === to || !text(message.id) || handoffs.some((item) => item.id === `i:${text(message.id)}`)) continue;
      const kind = text(message.kind);
      handoffs.push({ id: `i:${text(message.id)}`, kind: kind === 'reply' ? 'reply' : 'message', fromLaneId: from, toLaneId: to, atMs: activity.createdAtMs, label: kind === 'ask' ? '询问' : kind === 'reply' ? '回复' : '留言', failed: text(message.status) === 'failed' });
      events.push({ id: `i:${text(message.id)}`, laneId: from, atMs: activity.createdAtMs, tone: 'execute', actor: `${nameOf(from)} → ${nameOf(to)}`, text: `${kind === 'ask' ? '询问' : kind === 'reply' ? '回复' : '留言'}：${short(text(message.content), 40)}` });
      continue;
    }

    if (activity.kind === 'participant_status' && text(payload.status) === 'participant_joined') {
      const joinedId = text(payload.participantId) || activity.participantId || '';
      if (participantById.has(joinedId)) {
        joined.set(joinedId, activity.createdAtMs);
        involved.add(joinedId);
        const coordinator = participants.find((participant) => participant.collaborationRole === 'coordinator');
        const from = coordinator && coordinator.id !== joinedId ? coordinator.id : ORIGIN_LANE;
        handoffs.push({ id: `j:${joinedId}`, kind: 'recruit', fromLaneId: from, toLaneId: joinedId, atMs: activity.createdAtMs, label: '招募' });
        marks.push({ id: `j:${joinedId}`, laneId: joinedId, kind: 'recruit', atMs: activity.createdAtMs, label: '加入 Room' });
        events.push({ id: `j:${joinedId}`, laneId: joinedId, atMs: activity.createdAtMs, tone: 'origin', actor: nameOf(from), text: `招募 ${nameOf(joinedId)} 加入 Room` });
      }
      continue;
    }

    const toolName = text(payload.toolName || payload.toolId);
    const toolCallId = text(payload.toolCallId);
    const sourceEventType = text(payload.sourceEventType);
    if (toolName && toolCallId && sourceEventType === 'tool_finished' && !toolCalls.has(toolCallId) && laneId !== ORIGIN_LANE) {
      toolCalls.add(toolCallId);
      const failed = activity.status === 'failed' || payload.isError === true;
      if (failed) failedTools += 1;
      const op = text(record(payload.arguments).op);
      const labelText = toolLabel(toolName, op);
      marks.push({ id: `t:${toolCallId}`, laneId, kind: failed ? 'tool_failed' : 'tool', atMs: activity.updatedAtMs ?? activity.createdAtMs, label: `${labelText}${failed ? ' 失败' : ''}` });
      events.push({ id: `t:${toolCallId}`, laneId, atMs: activity.updatedAtMs ?? activity.createdAtMs, tone: failed ? 'fail' : 'execute', actor: nameOf(laneId), text: `${failed ? '工具失败' : '工具完成'} · ${labelText}` });
      if (toolName === 'room_partner' && op === 'add_participant' && !failed) {
        // The partner-added receipt is the authoritative join; this tool mark
        // already names who asked for it.
      }
      continue;
    }
    if (toolName && toolCallId && sourceEventType === 'tool_started' && activity.status === 'running' && laneId !== ORIGIN_LANE) {
      toolCalls.add(`${toolCallId}:open`);
    }
  }

  // Exact dispatch receipts precede the legacy participant fallback. A
  // participant may finish several dispatches long before the Root ends.
  for (const dispatchId of turn?.terminalDispatchIds ?? []) {
    const dispatch = dispatches.get(dispatchId);
    if (!dispatch) continue;
    dispatch.failed ||= (turn?.failedDispatchIds ?? []).includes(dispatchId);
    dispatch.aborted ||= (turn?.abortedDispatchIds ?? []).includes(dispatchId);
    if (dispatch.endMs !== undefined) continue;
    const failureReceipt = activities.find(activity => activity.kind === 'turn_failed'
      && text(activity.payload.dispatchId) === dispatchId);
    const completedMessage = messages.filter(message => message.dispatchId === dispatchId
      && message.completedAtMs !== undefined).at(-1);
    dispatch.endMs = failureReceipt?.createdAtMs ?? completedMessage?.completedAtMs ?? turn?.updatedAtMs ?? nowMs;
  }
  for (const participantId of turn?.terminalParticipantIds ?? []) {
    for (const dispatch of dispatches.values()) {
      if (dispatch.laneId !== participantId || dispatch.endMs !== undefined || live) continue;
      dispatch.endMs = turn?.updatedAtMs ?? nowMs;
      dispatch.failed = (turn?.failedParticipantIds ?? []).includes(participantId);
      dispatch.aborted = (turn?.abortedParticipantIds ?? []).includes(participantId);
    }
  }

  // 4. Segments: each dispatch window, with a waiting stretch while its own
  // children run (a coordinator is waiting on the planets it delegated to).
  const lastMs = Math.max(firstMs + 1, nowMs, ...activities.map((item) => item.updatedAtMs ?? item.createdAtMs), ...messages.map((item) => item.createdAtMs));
  const endMs = live ? Math.max(lastMs, nowMs) : Math.max(firstMs + 1, turn?.rootTerminalAtMs ?? 0, ...activities.map((item) => item.updatedAtMs ?? item.createdAtMs), ...messages.map((item) => item.createdAtMs));
  for (const dispatch of dispatches.values()) {
    const open = dispatch.endMs === undefined;
    const end = open ? (live ? endMs : Math.max(dispatch.startMs + 1, endMs)) : dispatch.endMs!;
    // A reviewer role describes capability, not an observed review. Only a
    // purpose/receipt that says verify may advance the review phase.
    const kind: CollabSegmentKind = PURPOSE_KIND[dispatch.purpose]
      ?? (participantById.get(dispatch.laneId)?.collaborationRole === 'coordinator' && !dispatch.parentId ? 'plan' : 'execute');
    if (kind === 'plan') reached.add('plan');
    if (kind === 'execute') reached.add('execute');
    if (kind === 'review') reached.add('review');
    if (kind === 'synthesize') reached.add('reply');
    if (dispatch.failed) {
      if (kind === 'plan' || kind === 'execute' || kind === 'review') phaseFailures.add(kind);
      if (kind === 'synthesize') phaseFailures.add('reply');
    }
    const children = [...dispatches.values()].filter((child) => child.parentId === dispatch.id).sort((a, b) => a.startMs - b.startMs);
    let cursor = dispatch.startMs;
    const push = (segmentKind: CollabSegmentKind, from: number, to: number, label: string, isOpen: boolean) => {
      if (to - from < 1) return;
      segments.push({ id: `s:${dispatch.id}:${segments.length}`, laneId: dispatch.laneId, kind: segmentKind, startMs: from, endMs: to, open: isOpen, label, failed: dispatch.failed && !isOpen && to === end, aborted: dispatch.aborted && !isOpen && to === end });
    };
    if (children.length) {
      const waitFrom = Math.max(cursor, children[0]!.startMs);
      const waitTo = Math.min(end, Math.max(...children.map((child) => child.endMs ?? end)));
      push(kind, cursor, waitFrom, dispatch.label, false);
      const names = [...new Set(children.map((child) => nameOf(child.laneId)))].join('、');
      // Delegating children does not prove the parent's Pi turn is waiting.
      push(kind, waitFrom, waitTo, `协调 ${names}`, open && live && waitTo >= end);
      cursor = waitTo;
      if (cursor < end) push(kind === 'plan' ? 'synthesize' : kind, cursor, end, kind === 'plan' ? '汇总答复' : dispatch.label, open);
      if (kind === 'plan' && cursor < end) reached.add('reply');
    } else {
      push(kind, cursor, end, dispatch.label, open && live);
    }
  }

  // 5. Satellites: Tool Agent runs under a partner Session.
  const lanes: CollabLane[] = [];
  const satelliteLanes: CollabLane[] = [];
  let satelliteCount = 0;
  for (const participant of participants) {
    const runs = (input.satellites?.[participant.sessionId] ?? []).filter((run) => {
      const at = run.startedAtMs ?? run.createdAtMs;
      return at >= firstMs - 1000 && at <= endMs + 1000;
    });
    const latest = new Map<string, AgentSubagentRunV1>();
    for (const run of runs) {
      const key = run.nodeId || run.id;
      const previous = latest.get(key);
      if (!previous || run.attemptNumber >= previous.attemptNumber) latest.set(key, run);
    }
    for (const run of latest.values()) {
      satelliteCount += 1;
      involved.add(participant.id);
      const laneId = `sat:${run.nodeId || run.id}`;
      const start = run.startedAtMs ?? run.createdAtMs;
      const running = run.state === 'queued' || run.state === 'running';
      const finish = running ? endMs : Math.max(start + 1, run.completedAtMs ?? run.updatedAtMs);
      const failed = ['failed', 'timed_out', 'aborted'].includes(run.state);
      satelliteLanes.push({
        id: laneId, kind: 'satellite', label: short(run.todoTask || run.task, 18) || 'Tool Agent', role: `卫星 · ${TEMPLATE[run.templateId] ?? run.templateId}`,
        parentId: participant.id, depth: 1 + Math.max(0, run.depth - 1), state: running ? 'working' : failed ? 'error' : 'done',
        status: running ? '运行中' : failed ? '失败' : '已返回', sessionId: run.childSessionId, runId: run.id,
        ...(run.launchDigest.modelProfile ? { model: run.launchDigest.modelProfile } : {}),
      });
      segments.push({ id: `s:${laneId}`, laneId, kind: 'satellite', startMs: start, endMs: finish, open: running, label: short(run.task, 30), failed });
      handoffs.push({ id: `sl:${laneId}`, kind: 'dispatch', fromLaneId: participant.id, toLaneId: laneId, atMs: start, label: '发射卫星' });
      if (!running) handoffs.push({ id: `sr:${laneId}`, kind: 'result', fromLaneId: laneId, toLaneId: participant.id, atMs: finish, label: failed ? '卫星失败' : '卫星返回', failed });
      events.push({ id: `sl:${laneId}`, laneId, atMs: start, tone: 'satellite', actor: `${nameOf(participant.id)} → 卫星`, text: `发射 ${TEMPLATE[run.templateId] ?? 'Tool Agent'}：${short(run.task, 36)}` });
      if (!running) events.push({ id: `sr:${laneId}`, laneId, atMs: finish, tone: failed ? 'fail' : 'satellite', actor: '卫星', text: failed ? `卫星失败${run.error ? `：${short(run.error, 36)}` : ''}` : '卫星返回结果' });
    }
  }

  // 6. Final reply.
  const finalMessage = [...messages].reverse().find((message) => message.role === 'assistant' && (message.postKind === 'result' || (!message.postKind && message.participantId === projection?.moderatorParticipantId)) && message.status === 'completed' && message.text.trim());
  const final = Boolean(graph?.final) || (!live && ['completed', 'failed'].includes(turn?.status ?? ''));
  const failed = graph?.final ? graph.final.status !== 'completed' : turn?.status === 'failed';
  const realGraphTasks = executableJevTasks(graph);
  if (failed) {
    for (const key of ['execute', 'review', 'accept', 'reply']) if (reached.has(key)) phaseFailures.add(key);
    if (!reached.has('execute') && !reached.has('review') && reached.has('plan')) phaseFailures.add('plan');
  }
  if (finalMessage && final) {
    const laneId = laneOf(finalMessage.participantId);
    handoffs.push({ id: `f:${finalMessage.id}`, kind: 'result', fromLaneId: laneId, toLaneId: ORIGIN_LANE, atMs: finalMessage.createdAtMs, label: '答复' });
    marks.push({ id: `f:${finalMessage.id}`, laneId: ORIGIN_LANE, kind: failed ? 'final_unfinished' : 'final', atMs: finalMessage.createdAtMs, label: failed ? '答复已发布 · 未完成' : '答复已发布' });
    events.push({ id: `f:${finalMessage.id}`, laneId: ORIGIN_LANE, atMs: finalMessage.createdAtMs, tone: failed ? 'fail' : 'done', actor: nameOf(laneId), text: failed ? '答复已发布，但本轮仍有未完成项' : '答复已发布到主 Room' });
    reached.add('reply');
    if (failed) phaseFailures.add('reply');
  }
  if (graph) {
    for (const task of realGraphTasks) if (task.state === 'done') reached.add('accept');
    // A completed Pi turn only means a result was submitted. Current task
    // outcomes remain authoritative while the final report is being prepared.
    if (realGraphTasks.some(task => task.state === 'failed')) {
      for (const key of ['execute', 'review', 'accept']) if (reached.has(key)) phaseFailures.add(key);
    }
    if (failed && reached.has('accept')) phaseFailures.add('accept');
  }

  // 7. Lanes: origin, then every planet that took part (or everyone when the
  // round is empty), each followed by its satellites.
  lanes.push({ id: ORIGIN_LANE, kind: 'origin', label: '团队调度', role: 'Room · 主线', depth: 0, state: live ? 'working' : final ? 'done' : stopped ? 'stopped' : 'idle', status: stopping ? '停止中，等待排空' : live ? '协作中' : final ? '本轮结束' : stopped ? '已停止' : '待命' });
  const shown = participants.filter((participant) => involved.has(participant.id) || participant.status === 'active');
  for (const participant of shown) {
    const own = [...dispatches.values()].filter((dispatch) => dispatch.laneId === participant.id);
    const current = own.find((dispatch) => dispatch.endMs === undefined);
    const lastSegment = segments.filter((segment) => segment.laneId === participant.id).sort((a, b) => a.endMs - b.endMs).at(-1);
    const state: CollabLaneState = !own.length ? 'idle'
      : stopping && current ? 'waiting' : stopped && current ? 'stopped'
        : current && live ? lastSegment?.kind === 'wait' ? 'waiting' : lastSegment?.kind === 'review' ? 'reviewing' : lastSegment?.kind === 'plan' || lastSegment?.kind === 'synthesize' ? 'thinking' : 'working'
          : own.some((dispatch) => dispatch.failed) ? 'error' : own.some((dispatch) => dispatch.aborted) ? 'stopped' : 'done';
    lanes.push({
      id: participant.id, kind: 'partner', label: roomPlanetName(participant.ordinal), ordinal: participant.ordinal,
      role: roleLabel(participant), depth: 0, state,
      status: stopping && current ? '停止中，等待排空' : state === 'idle' ? (participant.status === 'active' ? '本轮未参与' : '已离开') : state === 'done' ? '已交回' : state === 'error' ? '有失败' : state === 'stopped' ? '已停止' : lastSegment?.label ?? '进行中',
      sessionId: participant.sessionId,
      ...(models.get(participant.id) ? { model: models.get(participant.id)! } : {}),
      ...(joined.get(participant.id) ? { joinedAtMs: joined.get(participant.id)! } : {}),
    });
    lanes.push(...satelliteLanes.filter((lane) => lane.parentId === participant.id));
  }

  // Origin carries a thin "Root open" band so the user's round is visible.
  segments.push({ id: 's:origin', laneId: ORIGIN_LANE, kind: 'origin', startMs: firstMs, endMs: endMs, open: live, label: '本轮协作' });

  events.sort((a, b) => a.atMs - b.atMs);
  handoffs.sort((a, b) => a.atMs - b.atMs);
  if (userMessage) {
    const firstDispatch = handoffs.find((handoff) => handoff.kind === 'dispatch' && handoff.fromLaneId === ORIGIN_LANE);
    if (firstDispatch) firstDispatch.kind = 'request';
  }

  const seenWork = new Set([
    ...(room.workItems ?? []).filter((item) => item.rootTurnId === rootId).map((item) => item.id),
    ...activities.filter((item) => text(item.payload.activityKind) === 'work').map((item) => text(item.payload.workItemId)).filter(Boolean),
    ...[...dispatches.values()].map((dispatch) => dispatch.workItemId).filter(Boolean),
  ]);
  const taskMap = new Map<string, CollabTask>();
  for (const activity of activities) {
    if (text(activity.payload.activityKind) !== 'work') continue;
    const work = record(activity.payload.work);
    const id = text(activity.payload.workItemId) || text(work.id);
    if (!id) continue;
    const previous = taskMap.get(id);
    taskMap.set(id, { id, objective: text(work.objective) || previous?.objective || id,
      ownerLaneId: text(work.currentOwnerParticipantId) || previous?.ownerLaneId || '',
      state: text(work.state) || text(activity.payload.phase) || previous?.state || '',
      expectedOutput: text(work.expectedOutput) || previous?.expectedOutput || '',
      acceptance: Array.isArray(work.acceptanceCriteria) ? work.acceptanceCriteria.filter((v): v is string => typeof v === 'string') : previous?.acceptance ?? [],
      result: text(work.resultSummary) || previous?.result || '',
    });
  }
  for (const item of room.workItems ?? []) {
    if (item.rootTurnId !== rootId) continue;
    taskMap.set(item.id, { id: item.id, objective: item.objective, ownerLaneId: item.currentOwnerParticipantId || item.offeredToParticipantId,
      state: item.state, expectedOutput: item.expectedOutput, acceptance: item.acceptanceCriteria, result: item.resultSummary });
  }
  // Public Room history retains old WorkItems after a selective revision.
  // The current task list must use the graph's version boundary; their actual
  // dispatches remain available below as historical execution receipts.
  for (const task of graph?.historicalTasks ?? []) taskMap.delete(task.id);
  if (graph) for (const task of graph.tasks) {
    const stage = jevTaskStage(task, graph);
    taskMap.set(task.id, {
      id: task.id, objective: task.objective, ownerLaneId: task.ownerId, state: stage,
      stateLabel: JEV_TASK_STAGE_LABELS[stage],
      waitingOn: ['done', 'failed', 'cancelled', 'superseded'].includes(stage) ? [] : graph.edges
        .filter(edge => edge.dependent === task.id && edge.kind !== 'context')
        .map(edge => graph.tasks.find(item => item.id === edge.prerequisite))
        .filter(item => item && item.state !== 'done').map(item => item!.objective),
      expectedOutput: task.expectedOutput, acceptance: task.acceptance, result: task.result,
    });
  }
  const tasks = [...taskMap.values()];
  const taskDispatches = [...dispatches.values()].map(dispatch => ({
    id: dispatch.id, taskId: dispatch.workItemId, fromLaneId: dispatch.sourceLaneId, toLaneId: dispatch.laneId,
    objective: taskMap.get(dispatch.workItemId)?.objective || jevEffects.get(dispatch.id)?.objective || dispatch.label,
    state: dispatch.failed ? 'failed' : dispatch.aborted ? 'cancelled' : dispatch.endMs !== undefined ? 'submitted' : live ? 'active' : stopped ? 'cancelled' : 'unknown',
    atMs: dispatch.startMs,
  }));
  const totalWork = graph ? realGraphTasks.length : seenWork.size;
  const accepted = graph ? realGraphTasks.filter((task) => task.state === 'done').length : marks.filter((mark) => mark.kind === 'accept').length;
  const running = lanes.filter((lane) => ['working', 'thinking', 'reviewing'].includes(lane.state) && lane.kind !== 'origin').length;
  const focusLane = [...handoffs].reverse().find((handoff) => !handoff.failed)?.toLaneId ?? ORIGIN_LANE;
  return {
    id: `room:${room.id}:${rootId}`,
    title: short(userMessage?.text ?? room.title, 60),
    lanes,
    segments,
    handoffs,
    marks,
    events,
    phases: collabPhasesFromEvidence({ reached, final, failed: phaseFailures }).map(phase => (
      phase.key === 'accept' && graph && accepted < totalWork && phase.state !== 'failed'
        ? { ...phase, state: final ? 'failed' : 'current' }
        : phase
    )),
    tasks,
    dispatches: taskDispatches,
    startMs: firstMs,
    endMs,
    live,
    final,
    stopped,
    stopping,
    failed,
    scope: 'room',
    counts: { accepted, total: Math.max(totalWork, accepted), running, tools: toolCalls.size - [...toolCalls].filter((id) => id.endsWith(':open')).length, failed: failedTools, satellites: satelliteCount },
    focus: { laneId: focusLane, label: short(userMessage?.text ?? '本轮任务', 14), state: final ? 'done' : live ? 'moving' : 'held' },
  };
}

const TEMPLATE: Record<string, string> = { researcher: '研究员', planner: '规划员', worker: '执行者', reviewer: '审阅者', delegate: '委派者' };

function roleLabel(participant: RoomParticipant): string {
  return ROLE_LABELS[participant.collaborationRole ?? 'implementer'] ?? '协作伙伴';
}

/** Jev snapshots may retain a synthetic objective node above the executable
 *  tasks. It has no owner or accepted turn of its own; counting it would make
 *  the Room KPI claim one extra unfinished task. */
function executableJevTasks(graph?: JevSnapshot | null): JevSnapshot['tasks'] {
  const tasks = graph?.tasks ?? [];
  const executedTaskIds = new Set((graph?.effects ?? [])
    .filter((effect) => effect.operation === 'dispatch' && text(effect.request.purpose) === 'execute')
    .map((effect) => text(effect.request.taskId))
    .filter(Boolean));
  return tasks.filter((task) => !(task.parentId === '' && !task.acceptedTurnId
    && tasks.some((candidate) => candidate.parentId === task.id)
    && !executedTaskIds.has(task.id)));
}

const TOOL_LABELS: Record<string, string> = {
  room_partner: '行星协调', agents: '子 Agent 编排', workspace_shell: '终端命令', bash: '终端命令', read: '读取文件', write: '写入文件', edit: '编辑文件',
  workspace_read: '读取文件', workspace_write: '写入文件', workspace_edit: '编辑文件', grep: '搜索内容', find: '查找文件', ls: '浏览目录', browser: '浏览器操作', todo: 'Todo',
};
const PARTNER_OPS: Record<string, string> = {
  list: '查看名册', add_participant: '招募伙伴', remove_participant: '移出伙伴', delegate: '委派任务', delegate_batch: '并行委派', retry: '重新委派',
  post: '发布结果', collect: '收集结果', wait: '等待结果', accept: '验收通过', return: '退回返修', peer_list: '查看伙伴', peer_send: '给伙伴留言', peer_ask: '询问伙伴', peer_reply: '回复伙伴',
  plan_submit: '提交方案', result_submit: '提交任务结果', verification_submit: '提交复核结果', final_submit: '提交最终答复',
};
function toolLabel(toolName: string, op: string): string {
  const base = TOOL_LABELS[toolName] ?? toolName;
  const opLabel = toolName === 'room_partner' ? PARTNER_OPS[op] ?? op : op;
  return opLabel ? `${base} · ${opLabel}` : base;
}

function byTime<T extends { createdAtMs: number; sequence?: number }>(a: T, b: T): number {
  return a.createdAtMs - b.createdAtMs || (a.sequence ?? 0) - (b.sequence ?? 0);
}
function isMachine(value: string): boolean {
  return /^[a-z_]+$/u.test(value.trim());
}
function short(value: string, limit: number): string {
  const clean = value.replace(/\s+/gu, ' ').trim();
  return clean.length > limit ? `${clean.slice(0, limit - 1)}…` : clean;
}
function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}
function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
