/** Constructed fixtures only: no personal Room, Session or provider data. */
import type { RoomFocusPartner, RoomFocusProjection, RoomFocusWorkItem } from '@/paw-os/apps/room-focus-projection';
import type { RoomActivityProjection, RoomTurnProjection } from '@/contracts/room-reducer';
import type { RoomParticipant } from '@/features/rooms/room-types';

export function visualWork(id: string, overrides: Partial<RoomFocusWorkItem> = {}): RoomFocusWorkItem {
  return { id, source: 'work-item', objective: `核验 ${id}`, state: 'running',
    ownerParticipantId: 'mars', accountableParticipantId: 'earth', reviewRequired: true,
    acceptanceCriteria: ['实际回执与预期一致'], evidence: [], updatedAtMs: 100, ...overrides };
}
export function visualPartner(id = 'mars', overrides: Partial<RoomFocusPartner> = {}): RoomFocusPartner {
  return { participantId: id, sessionId: `s-${id}`, celestialName: id === 'earth' ? 'Earth' : 'Mars',
    displayName: id === 'earth' ? '最终汇总' : '前端实现', collaborationRole: id === 'earth' ? 'coordinator' : 'implementer',
    state: 'running', currentAction: '正在核对消息顺序', ownedWorkItemIds: [], unread: false, ...overrides };
}
export function visualFocus(workItems: RoomFocusWorkItem[] = [visualWork('a')]): RoomFocusProjection {
  return { goal: { title: '修复协作状态', description: '', rootId: 'root', state: 'running' },
    partners: [visualPartner('earth'), visualPartner()], workItems,
    flow: [], handoffs: [], rootEvidence: [], counts: { active: 1, blocked: 0, review: 0, completed: 0 } };
}
export function visualTurn(overrides: Partial<RoomTurnProjection> = {}): RoomTurnProjection {
  return { id: 'root', status: 'running', messageIds: [], activityIds: [], participantIds: ['earth', 'mars'],
    dispatchIds: ['d'], dispatchParticipantIds: { d: 'mars' }, createdAtMs: 10, updatedAtMs: 100, ...overrides };
}
export function visualActivity(id: string, overrides: Partial<RoomActivityProjection> = {}): RoomActivityProjection {
  return { id, turnId: 'root', participantId: 'mars', sourceSessionId: 's-mars', kind: 'route_decision',
    status: 'running', summary: 'route_decision', payload: { dispatchId: 'd', targetParticipantId: 'mars', reason: 'moderator' },
    sequence: 1, createdAtMs: 10, ...overrides };
}
export const visualRoster: { participants: RoomParticipant[] } = { participants: ['earth', 'mars'].map((id, ordinal) => ({
  id, sessionId: `s-${id}`, roleId: `role-${id}`, roleVersion: '1', displayName: id, status: 'active', ordinal,
})) };
