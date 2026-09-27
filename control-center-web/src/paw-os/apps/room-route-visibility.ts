import type { RoomActivityProjection, RoomTurnProjection } from '@/contracts/room-reducer';
import type { RoomSummary } from '@/features/rooms/room-types';

export type RoomRouteDecisionState = 'rule' | 'explicit' | 'selected' | 'abstained' | 'unavailable' | 'stale' | 'invalid' | 'unknown';
export type RoomRouteExecutionState = 'planned' | 'started' | 'returned' | 'failed' | 'stopped' | 'unknown';
export interface RoomRouteVisibility {
  id: string;
  rootId: string;
  dispatchId: string;
  targetId: string;
  targetName: string;
  canOpen: boolean;
  source: 'jev' | 'explicit' | 'room';
  decision: RoomRouteDecisionState;
  decisionLabel: string;
  execution: RoomRouteExecutionState;
  executionLabel: string;
  confidence?: number;
  sourceRevision?: string;
  startedReceiptId?: string;
  executionReceiptId?: string;
  updatedAtMs: number;
  raw: Record<string, unknown>;
}
const text = (value: unknown): string => typeof value === 'string' ? value : '';
const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const explicit = new Set(['explicit_invite', 'mention', 'explicit_mention', 'work_item_owner']);
const decisionLabels: Record<RoomRouteDecisionState, string> = {
  rule: 'Room 规则路由', explicit: '按明确指定分派', selected: 'Jev 已建议接收者',
  abstained: 'Jev 弃权 · 沿用原路由', unavailable: 'Jev 不可用 · 沿用原路由',
  stale: 'Jev 建议已失效', invalid: 'Jev 回执待核对', unknown: 'Jev 决策状态未知',
};
const executionLabels: Record<RoomRouteExecutionState, string> = {
  planned: '已记录路由 · 尚无开始回执', started: '已有执行回执',
  returned: '执行已返回 · 验收另计', failed: '执行失败', stopped: '执行已停止', unknown: '执行归属待同步',
};

/** Reads the existing route_decision payload (including optional .jev from the
 * supplied reference). No provider request, HTTP endpoint, route mutation, new
 * event type, permission, or automatic retries. A Choice is never execution. */
export function projectRoomRoutes(
  activities: readonly RoomActivityProjection[], room: Pick<RoomSummary, 'participants'>,
  turn?: RoomTurnProjection,
): RoomRouteVisibility[] {
  if (!turn) return [];
  const scoped = activities.filter((activity) => activity.turnId === turn.id);
  const routes = scoped.filter((activity) => activity.kind === 'route_decision');
  const byDispatch = new Map<string, RoomActivityProjection[]>();
  for (const item of scoped) {
    const key = text(item.payload.dispatchId || item.payload.childDispatchId);
    if (!key || item.kind === 'route_decision') continue;
    const bucket = byDispatch.get(key) ?? [];
    bucket.push(item); byDispatch.set(key, bucket);
  }
  const seen = new Set<string>();
  return routes.filter((route) => {
    if (seen.has(route.id)) return false;
    seen.add(route.id); return true;
  }).map((route) => {
    const payload = route.payload;
    const targetIds = Array.isArray(payload.selectedParticipantIds) ? payload.selectedParticipantIds.filter((id): id is string => typeof id === 'string') : [];
    const targetId = text(payload.targetParticipantId) || (targetIds.length === 1 ? targetIds[0] : '') || route.participantId || '';
    const dispatchId = text(payload.dispatchId) || text(payload.childDispatchId);
    const participant = room.participants.find((item) => item.id === targetId);
    const jev = record(payload.jev);
    const source = explicit.has(text(payload.reason)) ? 'explicit' : Object.keys(jev).length || payload.reason === 'jev' ? 'jev' : 'room';
    const confidence = typeof jev.confidence === 'number' && Number.isFinite(jev.confidence) && jev.confidence >= 0 && jev.confidence <= 1 ? jev.confidence : undefined;
    let decision: RoomRouteDecisionState = source === 'explicit' ? 'explicit' : source === 'room' ? 'rule' : 'unknown';
    if (source === 'jev') {
      if (jev.status === 'selected') {
        decision = confidence !== undefined && confidence >= .70 && text(jev.choice) !== 'unknown'
          && Boolean(targetId) && jev.choice === targetId ? 'selected' : 'invalid';
      } else if (jev.status === 'abstained') decision = 'abstained';
      else if (jev.status === 'unavailable') decision = 'unavailable';
      else if (['stale_decision', 'superseded', 'superseded_evaluation'].includes(text(jev.status))) decision = 'stale';
    }
    const mappedOwner = dispatchId ? turn.dispatchParticipantIds?.[dispatchId] : undefined;
    const ownerConsistent = !mappedOwner || mappedOwner === targetId;
    // Only events bound to this exact dispatch/Root may prove it started. A tool
    // result from the same participant's ordinary Session cannot advance it.
    const evidence = dispatchId && ownerConsistent ? (byDispatch.get(dispatchId) ?? []).filter((item) => (
      item.participantId === targetId && (!participant || item.sourceSessionId === participant.sessionId)
    )) : [];
    const started = evidence.find((item) => ['tool_started', 'tool_progress', 'tool_finished', 'turn_started'].includes(text(item.payload.sourceEventType))
      || (item.kind === 'participant_status' && ['working', 'responding', 'analyzing'].includes(text(item.payload.status))));
    let execution: RoomRouteExecutionState = !dispatchId || !ownerConsistent ? 'unknown' : started ? 'started' : 'planned';
    if (dispatchId && ownerConsistent) {
      if (turn.failedDispatchIds?.includes(dispatchId)) execution = 'failed';
      else if (turn.abortedDispatchIds?.includes(dispatchId)) execution = 'stopped';
      else if (turn.terminalDispatchIds?.includes(dispatchId)) execution = 'returned';
      // Participant terminal alone is insufficient: one participant may run
      // several dispatches, including a later wake or retry.
    }
    const terminalReceipt = evidence.find((item) => item.kind === 'turn_failed' || item.kind === 'turn_completed');
    return {
      id: route.id, rootId: turn.id, dispatchId, targetId,
      targetName: participant?.displayName || text(payload.targetDisplayName) || '接收者待同步',
      canOpen: Boolean(participant?.sessionId), source, decision, decisionLabel: decisionLabels[decision],
      execution, executionLabel: executionLabels[execution], confidence,
      sourceRevision: text(jev.sourceRevision) || undefined,
      startedReceiptId: started?.id,
      executionReceiptId: terminalReceipt?.id,
      updatedAtMs: Math.max(route.updatedAtMs ?? route.createdAtMs, ...evidence.map((item) => item.updatedAtMs ?? item.createdAtMs)),
      raw: payload,
    };
  });
}
