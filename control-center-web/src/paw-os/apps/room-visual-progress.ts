import type { RoomFocusProjection, RoomFocusWorkItem } from './room-focus-projection';

export type RoomProgressBucket = 'completed' | 'review' | 'working' | 'blocked' | 'failed' | 'stopped' | 'pending' | 'unknown';
export const roomProgressLabels: Record<RoomProgressBucket, string> = {
  completed: '已完成', review: '待复核', working: '进行中', blocked: '受阻',
  failed: '失败', stopped: '已停止', pending: '待开始', unknown: '待同步',
};
export const roomProgressOrder: readonly RoomProgressBucket[] = [
  'completed', 'review', 'working', 'blocked', 'failed', 'stopped', 'pending', 'unknown',
];
export interface RoomVisualProgress {
  total: number;
  completed: number;
  accepted: number;
  /** A ratio of known leaf WorkItems, never time, tokens or estimated effort. */
  fraction: number | null;
  leaves: RoomFocusWorkItem[];
  aggregates: RoomFocusWorkItem[];
  segments: { key: RoomProgressBucket; label: string; count: number; taskIds: string[] }[];
  incomplete: boolean;
  explanation: string;
}
export function roomProgressBucket(task: RoomFocusWorkItem): RoomProgressBucket {
  switch (task.state) {
    case 'completed': return 'completed';
    case 'review': return 'review';
    case 'running': return 'working';
    case 'blocked': return 'blocked';
    case 'failed': return 'failed';
    case 'stopped': return 'stopped';
    case 'waiting': case 'idle': return 'pending';
    default: return 'unknown';
  }
}
export function roomTaskAccepted(task: RoomFocusWorkItem): boolean {
  return task.source === 'work-item' && task.state === 'completed'
    && task.review?.operability === 'passed' && task.review?.requirement === 'satisfied';
}

/** Parent aggregation is deliberately not counted twice. A cancelled task stays
 * in the denominator, but never becomes successful work. Runtime dispatch rows
 * have no acceptance authority and are excluded. Cycles/conflicting duplicates
 * suppress a numeric ratio, not the underlying readable task records. */
export function buildRoomVisualProgress(focus: Pick<RoomFocusProjection, 'workItems'>): RoomVisualProgress {
  const explicit = focus.workItems.filter((item) => item.source === 'work-item');
  const byId = new Map<string, RoomFocusWorkItem>();
  let invalid = false;
  for (const task of explicit) {
    if (byId.has(task.id)) invalid = true;
    else byId.set(task.id, task);
  }
  const parents = new Set<string>();
  let missingParent = false;
  for (const task of byId.values()) {
    if (task.parentId) {
      if (byId.has(task.parentId)) parents.add(task.parentId);
      else missingParent = true;
    }
  }
  // Linear graph colouring; a deep chain must not recurse through the JS stack.
  const done = new Set<string>();
  for (const task of byId.values()) {
    const path = new Set<string>();
    let id: string | undefined = task.id;
    while (id && byId.has(id) && !done.has(id)) {
      if (path.has(id)) { invalid = true; break; }
      path.add(id); id = byId.get(id)?.parentId;
    }
    for (const key of path) done.add(key);
  }
  const leaves = [...byId.values()].filter((task) => !parents.has(task.id));
  const aggregates = [...byId.values()].filter((task) => parents.has(task.id));
  const segments = roomProgressOrder.map((key) => {
    const taskIds = leaves.filter((task) => roomProgressBucket(task) === key).map((task) => task.id);
    return { key, label: roomProgressLabels[key], count: taskIds.length, taskIds };
  });
  const completed = leaves.filter((task) => task.state === 'completed').length;
  const incomplete = invalid || missingParent;
  return {
    total: leaves.length, completed, accepted: leaves.filter(roomTaskAccepted).length,
    fraction: !incomplete && leaves.length ? completed / leaves.length : null,
    leaves, aggregates, segments, incomplete,
    explanation: invalid ? '任务结构存在重复或循环，暂不计算比例；原始任务仍可查看。'
      : missingParent ? '部分上层任务尚未同步，先显示已知执行项，不计算整体比例。'
        : !leaves.length ? '尚无可计数的工作项；执行轨迹仍可查看，不用转圈或假比例补齐。'
          : `按当前 ${leaves.length} 个末级工作项计数，不代表耗时或工作量${aggregates.length ? `；${aggregates.length} 个上层汇总项单列，不重复计数` : ''}。`,
  };
}

/** Stable codepoint excerpt, not an AI-written summary. Full wording is kept by
 * the caller in task detail/disclosure; no auto translation or silent rewrite. */
export function roomTextExcerpt(value: string, limit = 76): string {
  const text = value.replace(/\s+/gu, ' ').trim();
  const points = Array.from(text);
  return points.length <= limit ? text : `${points.slice(0, limit).join('')}…`;
}

const eventLabels: Record<string, string> = {
  turn_failed: '本轮执行失败', turn_completed: '本轮执行已结束',
  turn_aborted: '本轮已停止', participant_activity: '公开执行记录已更新',
  participant_status: '伙伴状态已更新', participant_delta: '正在生成回复',
  participant_message: '伙伴已返回消息', route_decision: '已记录任务分派',
  tool_started: '正在调用工具', tool_progress: '工具进展已更新',
  tool_finished: '工具已返回结果', tool_failed: '工具调用失败',
  cancellation_requested: '正在请求停止', cancellation_applied: '已收到停止回执',
  user_input_required: '等待你的回答', snapshot_required: '需要恢复协作状态',
};
export function roomPublicAction(raw: string, fallback = '等待新的执行回执'): string {
  const value = raw.trim();
  if (!value) return fallback;
  const recoveredTerminal = /^Partner Session (completed|failed|aborted); recovered from durable Runtime terminal event\b/iu.exec(value);
  if (recoveredTerminal) {
    return ({ completed: '伙伴执行已结束', failed: '伙伴执行失败', aborted: '伙伴执行已停止' } as Record<string, string>)[recoveredTerminal[1]!.toLowerCase()]
      + '（根据运行时回执恢复）';
  }
  return eventLabels[value] ?? (/^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/iu.test(value)
    ? '执行记录已更新，展开可查看原始事件' : value);
}
