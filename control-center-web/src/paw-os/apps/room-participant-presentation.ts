import type { RoomFocusPartner, RoomFocusProjection, RoomFocusState } from './room-focus-projection';
import { buildRoomVisualProgress, roomPublicAction, roomTextExcerpt } from './room-visual-progress';
import { roomPartnerStatusLabel } from './room-work-status';

export type RoomParticipantFreshness = 'live' | 'offline' | 'recovering' | 'paused' | 'last-known';
export interface RoomParticipantPresentation {
  id: string;
  name: string;
  identity: string;
  role: string;
  roles: string[];
  state: RoomFocusState;
  execution: string;
  task: string;
  taskId?: string;
  action: string;
  rawAction: string;
  total: number;
  completed: number;
  review: number;
  offered: number;
  canOpen: boolean;
}
const priority: Record<string, number> = { blocked: 0, failed: 1, running: 2, review: 3, waiting: 4, idle: 5, completed: 6, stopped: 7 };
export function roomRoleLabel(role?: string): string {
  if (role === 'coordinator') return '统筹';
  if (role === 'reviewer') return '复核';
  if (role === 'specialist') return '执行';
  if (role === 'researcher') return '调研';
  if (role === 'implementer') return '实现';
  return '协作伙伴';
}

/** Role and task assignment are separate from execution. An offered recipient,
 * reviewer or accountable coordinator must not inherit another owner's progress. */
export function presentRoomParticipant(
  partner: RoomFocusPartner, focus: RoomFocusProjection,
  freshness: RoomParticipantFreshness = 'live',
): RoomParticipantPresentation {
  const tasks = focus.workItems.filter((task) => task.ownerParticipantId === partner.participantId);
  const ordered = tasks.slice().sort((a, b) => (priority[a.state] ?? 8) - (priority[b.state] ?? 8));
  const progress = buildRoomVisualProgress(focus);
  const ownedLeaves = progress.leaves.filter((task) => task.ownerParticipantId === partner.participantId);
  const reviewTasks = focus.workItems.filter((task) => task.verifierParticipantId === partner.participantId);
  const roles = new Set([roomRoleLabel(partner.collaborationRole)]);
  if (focus.workItems.some((task) => task.accountableParticipantId === partner.participantId)) roles.add('统筹');
  if (tasks.length) roles.add('执行');
  if (reviewTasks.length) roles.add('复核');
  if (roles.size > 1) roles.delete('协作伙伴');
  const chosen = ordered[0] ?? reviewTasks.find((task) => task.state === 'review') ?? reviewTasks[0];
  const rawName = partner.displayName.trim();
  const name = rawName && !rawName.includes('尚未设置') && rawName !== partner.celestialName
    ? roomTextExcerpt(rawName, 24) : `${roomRoleLabel(partner.collaborationRole)}伙伴`;
  const prefix = freshness === 'live' ? '' : freshness === 'offline' ? '离线 · 上次' : freshness === 'paused' ? '暂停同步 · 上次' : freshness === 'recovering' ? '恢复中 · 上次' : '上次';
  const taskHeadline = chosen?.objective.split('```', 1)[0]?.trim();
  const publicAction = roomPublicAction(partner.currentAction);
  const actionHeadline = publicAction.split('```', 1)[0]?.trim();
  return {
    id: partner.participantId, name, identity: partner.celestialName,
    role: roomRoleLabel(partner.collaborationRole), roles: [...roles], state: partner.state,
    execution: `${prefix}${roomPartnerStatusLabel(partner.state)}`,
    task: chosen ? roomTextExcerpt(taskHeadline || '任务附带数据，详情请查看分工图', 64)
      : partner.collaborationRole === 'coordinator' ? '协调分工与汇总结果' : '暂无当前工作项',
    taskId: chosen?.id,
    action: roomTextExcerpt(actionHeadline && !/^[{[]/u.test(actionHeadline) ? actionHeadline : '执行记录已更新', 76),
    rawAction: partner.currentAction,
    total: ownedLeaves.length, completed: ownedLeaves.filter((task) => task.state === 'completed').length,
    review: ownedLeaves.filter((task) => task.state === 'review').length,
    offered: focus.workItems.filter((task) => !task.ownerParticipantId && task.offeredToParticipantId === partner.participantId).length,
    canOpen: Boolean(partner.sessionId),
  };
}
