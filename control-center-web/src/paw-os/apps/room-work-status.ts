import type { RoomProjectionState } from '@/contracts/room-reducer';
import { roomCollaborationRoleLabel } from '@/features/rooms/room-copy';
import type { RoomFocusProjection, RoomFocusState, RoomFocusWorkItem } from './room-focus-projection';

export type RoomWorkStatusState = 'syncing' | 'offline' | 'paused-view' | 'needs-input'
  | 'running' | 'waiting-partners' | 'review' | 'blocked' | 'failed' | 'stopping'
  | 'stopped' | 'completed' | 'awaiting-root' | 'idle';
export interface RoomWorkStatus {
  state: RoomWorkStatusState;
  headline: string;
  detail: string;
  animate: boolean;
  live: boolean;
  updatedAtMs: number;
  completed: number;
  total: number;
  review: number;
  executingParticipantIds: string[];
  action: 'sync' | 'answer' | 'inspect';
}
export interface RoomWorkStatusInput {
  focus: RoomFocusProjection;
  projection?: RoomProjectionState;
  recoveryState: 'recovering' | 'failed' | 'synced';
  visible: boolean;
  stopping?: boolean;
  pendingInput?: boolean;
}

const stateLabels: Record<RoomFocusState, string> = {
  idle: '待命', waiting: '等待', running: '执行中', review: '等待复核', blocked: '受阻',
  completed: '本轮执行结束', failed: '本轮执行失败', stopped: '已停止', disconnected: '不可用',
};
export function roomPartnerStatusLabel(state: RoomFocusState): string {
  return stateLabels[state];
}
export function roomWorkStatusLabel(task: RoomFocusWorkItem): string {
  if (!task.ownerParticipantId && task.offeredToParticipantId) return '待接收';
  if (task.state === 'completed') {
    if (task.source === 'runtime') return '执行已返回';
    return task.review?.operability === 'passed' && task.review?.requirement === 'satisfied'
      ? '已验收' : '工作项已完成';
  }
  if (task.state === 'running') return '工作项进行中';
  return stateLabels[task.state];
}

/** A pure presentation projection. Room Root, partner execution, WorkItem
 * acceptance and stream freshness stay separate; this never changes any of them.
 * No timers, made-up ETA, completion by prose, or new execution loop. */
export function buildRoomWorkStatus(input: RoomWorkStatusInput): RoomWorkStatus {
  const { focus, projection, recoveryState, visible, stopping, pendingInput } = input;
  const turn = projection?.turnsById[focus.goal.rootId];
  const activities = (turn?.activityIds ?? [])
    .map((id) => projection?.activitiesById[id]).filter((item) => item !== undefined);
  const explicit = focus.workItems.filter((item) => item.source === 'work-item');
  const review = explicit.filter((item) => item.state === 'review');
  const blocked = explicit.filter((item) => ['blocked', 'failed'].includes(item.state));
  const terminalIds = new Set([
    ...(turn?.terminalParticipantIds ?? []), ...(turn?.failedParticipantIds ?? []),
    ...(turn?.abortedParticipantIds ?? []),
  ]);
  const running = turn?.status === 'running' ? focus.partners.filter((partner) =>
    turn.participantIds.includes(partner.participantId)
    && !terminalIds.has(partner.participantId) && partner.state === 'running') : [];
  const latest = [...activities].sort((a, b) =>
    (b.sequence ?? b.createdAtMs) - (a.sequence ?? a.createdAtMs))[0];
  const publicAction = latest?.summary.trim() || '';
  const updatedAtMs = Math.max(0, turn?.updatedAtMs ?? 0,
    ...activities.map((item) => item.updatedAtMs ?? item.createdAtMs),
    ...focus.workItems.map((item) => item.updatedAtMs));
  const live = Boolean(visible && recoveryState === 'synced' && projection && !projection.needsSnapshot);
  const base = {
    live, updatedAtMs, completed: explicit.filter((item) => item.state === 'completed').length,
    total: explicit.length, review: review.length,
    executingParticipantIds: live ? running.map((item) => item.participantId) : [],
  };
  const status = (state: RoomWorkStatusState, headline: string, detail: string,
    action: RoomWorkStatus['action'] = 'inspect', animate = false): RoomWorkStatus =>
    ({ ...base, state, headline, detail, action, animate: live && animate });
  if (recoveryState === 'failed') return status('offline', '连接中断 · 显示上次状态',
    '草稿和已读记录保留；重新同步只读取状态，不会重新派发。', 'sync');
  if (!visible) return status('paused-view', '实时显示已暂停', '回到此页面后恢复同步；后台任务不会因此停止。');
  if (recoveryState !== 'synced' || !projection || projection.needsSnapshot)
    return status('syncing', '正在同步协作状态', '正在核对事件与快照；上次状态不代表此刻仍在执行。', 'sync');
  if (stopping) return status('stopping', '正在停止协作', '等待执行方的停止回执；此时尚未确认全部停止。');
  if (turn?.status === 'aborted') return status('stopped', '本轮已停止', '保留已有结果和证据，未执行的工作不会标记完成。');
  if (turn?.status === 'failed') return status('failed', '本轮执行失败', turn.failure || '查看任务或实际 Session，核对失败原因。');
  const question = projection.pendingUserQuestion;
  if (pendingInput || (question && question.rootId === focus.goal.rootId))
    return status('needs-input', '需要你的回答', question?.prompt || '请在下方回答现有问题，随后继续本轮。', 'answer');
  const detail = running.map((partner) => `${partner.celestialName}（${roomCollaborationRoleLabel(partner.collaborationRole)}）· ${partner.currentAction}`).join('；');
  if (blocked.length) return status('blocked', `${blocked.length} 个工作项需要处理`,
    `${blocked[0]?.blocker?.reason || blocked[0]?.objective}${running.length ? `；另有 ${running.length} 位伙伴仍在执行` : ''}`);
  if (running.length) {
    const coordinator = focus.partners.find((partner) => partner.collaborationRole === 'coordinator');
    const waitingForPartners = Boolean(coordinator && terminalIds.has(coordinator.participantId)
      && running.every((partner) => partner.participantId !== coordinator.participantId));
    return status(waitingForPartners ? 'waiting-partners' : 'running', waitingForPartners
      ? `等待伙伴结果 · ${running.length} 位执行中` : `${running.length} 位伙伴正在执行`, detail, 'inspect', true);
  }
  if (review.length) return status('review', `${review.length} 个工作项等待复核`,
    `${review[0]?.objective}；执行结束与验收完成分别记录。`);
  if (turn?.status === 'completed') return status('completed', '本轮执行已结束',
    explicit.some((item) => !['completed', 'stopped'].includes(item.state))
      ? '仍有未收束工作项，请展开核对；不会把执行结束当作全部验收。'
      : explicit.length ? `${base.completed} / ${base.total} 工作项已完成，结果与证据可展开查看。`
        : 'Root 已有终态回执；此轮没有可核对的工作项计数。');
  if (turn?.status === 'running') return status('awaiting-root', '等待协作回执',
    publicAction || '目前没有可确认的执行节点，等待统筹或伙伴的新回执。');
  if (turn?.status === 'queued') return status('idle', '请求已排队', '尚未收到执行开始的证据。');
  return status('idle', '暂无执行中的协作', publicAction || '发送任务后在这里查看分工、公开进展与结果。');
}
