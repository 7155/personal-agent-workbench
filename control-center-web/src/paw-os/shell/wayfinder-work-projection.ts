/**
 * Wayfinder work projection — the desktop's read-only view of recent Sessions
 * and Rooms at desktop density.
 *
 * The raw directory is a web-length list: every repeated goal prompt and every
 * A legacy Room partner clone with a numbered suffix arrives as its own record. A desktop
 * home must stay a short, grouped, bounded surface, so this module folds the
 * raw records before anything renders:
 *
 * 1. Room partner Sessions (`roomParticipant`) never appear as rows. They fold
 *    into their existing Room's row; a missing Room never becomes a synthetic
 *    clickable object.
 * 2. Records that repeat the same goal copy (same kind + title + project)
 *    collapse into the newest record; older runs stay reachable as `repeats`.
 * 3. Rows land in three recency buckets — 今天 / 本周 / 更早 — each with a
 *    small preview count so the first screen shows recent useful work, not an
 *    endless page. The stale bucket is meant to rest collapsed.
 *
 * Pure data → data. The owning component decides fetch, expansion and clicks.
 */

import { roomPlanetName } from '@/features/rooms/room-participant-identity';
export type WayfinderWorkKind = 'session' | 'room';
export type WayfinderWorkActivity = 'running' | 'attention' | 'idle' | 'unknown';

export type WayfinderWorkSessionSource = {
  id: string;
  title: string;
  status: string;
  updatedAtMs: number;
  workspaceRoots?: string[];
  lastMessagePreview?: string;
  lastTerminalTurnId?: string;
  roomParticipant?: { roomId?: string } | null;
};

export type WayfinderWorkRoomSource = {
  id: string;
  title: string;
  status?: string;
  updatedAtMs: number;
  workspaceRoots?: string[];
  workItems?: Array<{
    state: 'queued' | 'active' | 'review' | 'blocked' | 'done' | 'failed' | 'cancelled';
    objective?: string;
    resultSummary?: string;
    blocker?: { reason?: string };
    updatedAtMs: number;
  }>;
  participants?: Array<{ displayName?: string; ordinal?: number; status?: string }>;
};

export type WayfinderWorkRepeat = {
  id: string;
  kind: WayfinderWorkKind;
  updatedAtMs: number;
};

export type WayfinderWorkItem = {
  key: string;
  projectKey: string;
  /** Real workspace roots projected from the directory; never a Finder/Git mirror. */
  workspaceRoots: string[];
  kind: WayfinderWorkKind;
  id: string;
  title: string;
  project: string;
  updatedAtMs: number;
  activity: WayfinderWorkActivity;
  /** Independent live Runtime signal; may coexist with an attention state. */
  runtimeRunning: boolean;
  statusLabel: string;
  detail: string;
  /** Recorded Room task counts only; Session messages do not imply a percent. */
  progress?: { completed: number; total: number };
  /** Room planet names in ordinal order — rendered side by side, never as rows. */
  agents: string[];
  /** Older records with the same goal copy, newest first. */
  repeats: WayfinderWorkRepeat[];
};

export type WayfinderWorkBucketId = 'today' | 'week' | 'earlier';

export type WayfinderWorkBucket = {
  id: WayfinderWorkBucketId;
  label: string;
  items: WayfinderWorkItem[];
  /** How many rows the first screen shows before the bucket asks to expand. */
  previewCount: number;
};

export type WayfinderWorkProject = {
  id: string;
  label: string;
  workspaceRoots: string[];
  items: WayfinderWorkItem[];
  buckets: WayfinderWorkBucket[];
  sessionCount: number;
  roomCount: number;
  runningCount: number;
  attentionCount: number;
};

export type WayfinderWorkView = {
  buckets: WayfinderWorkBucket[];
  projects: WayfinderWorkProject[];
  /** Rows after folding — what the desktop can actually show. */
  rowCount: number;
  /** Raw records absorbed by folding (partner clones + repeated goal copy). */
  foldedCount: number;
};

const BUCKET_LABELS: Record<WayfinderWorkBucketId, string> = {
  today: '今天',
  week: '本周',
  earlier: '更早',
};

const BUCKET_PREVIEW: Record<WayfinderWorkBucketId, number> = {
  today: 5,
  week: 3,
  earlier: 0,
};

export function projectWayfinderWork({
  nowMs,
  query = '',
  roomStatusFresh = true,
  rooms,
  sessionStatusFresh = true,
  sessions,
}: {
  nowMs: number;
  query?: string;
  roomStatusFresh?: boolean;
  rooms: readonly WayfinderWorkRoomSource[];
  sessionStatusFresh?: boolean;
  sessions: readonly WayfinderWorkSessionSource[];
}): WayfinderWorkView {
  const liveRooms = rooms.filter((room) => room.status !== 'archived');
  const liveSessions = sessions.filter((session) => session.status !== 'archived');
  const partnerSessions = liveSessions.filter((session) => session.roomParticipant?.roomId);
  const plainSessions = liveSessions.filter((session) => !session.roomParticipant?.roomId);

  const runningRoomIds = busyRoomIds(partnerSessions, sessionStatusFresh);
  const roomRows = liveRooms.map((room) => roomRow(room, {
    recordFresh: roomStatusFresh,
    runtimeFresh: sessionStatusFresh,
    running: runningRoomIds.has(room.id),
  }));
  const sessionRows = plainSessions.map((session) => sessionRow(session, sessionStatusFresh));

  let foldedCount = partnerSessions.length;
  const deduped = new Map<string, WayfinderWorkItem>();
  for (const row of [...sessionRows, ...roomRows]
    .sort((left, right) => right.updatedAtMs - left.updatedAtMs)) {
    /* Live and attention rows remain separately reachable. Folding them into a
       newer quiet row makes the visible title/click target disagree with the
       Runtime object that is actually working or needs attention. */
    const foldKey = row.activity === 'idle' || row.activity === 'unknown'
      ? `${row.kind}\u0001${normalizedTitle(row.title)}\u0001${row.projectKey}`
      : `${row.kind}\u0001${row.id}`;
    const leader = deduped.get(foldKey);
    if (!leader) {
      deduped.set(foldKey, row);
      continue;
    }
    leader.repeats.push({ id: row.id, kind: row.kind, updatedAtMs: row.updatedAtMs });
    foldedCount += 1;
  }

  const matched = [...deduped.values()].filter((item) => matchesWayfinderWork(item, query));

  const buckets: WayfinderWorkBucket[] = (['today', 'week', 'earlier'] as const)
    .map((id) => ({
      id,
      label: BUCKET_LABELS[id],
      items: matched.filter((item) => bucketFor(item.updatedAtMs, nowMs) === id),
      previewCount: BUCKET_PREVIEW[id],
    }))
    .filter((bucket) => bucket.items.length > 0);

  const projects = [...new Set(matched.map((item) => item.projectKey))]
    .map((id) => {
      const items = matched.filter((item) => item.projectKey === id);
      const workspaceRoots = [...new Set(items.flatMap((item) => item.workspaceRoots))].sort();
      return {
        id,
        label: items[0]?.project || '未绑定项目',
        workspaceRoots,
        items,
        buckets: bucketizeWayfinderWork(items, nowMs),
        sessionCount: items.filter((item) => item.kind === 'session').length,
        roomCount: items.filter((item) => item.kind === 'room').length,
        runningCount: items.filter((item) => item.runtimeRunning).length,
        attentionCount: items.filter((item) => item.activity === 'attention').length,
      };
    })
    .sort((left, right) => (right.items[0]?.updatedAtMs ?? 0) - (left.items[0]?.updatedAtMs ?? 0));

  return { buckets, projects, rowCount: matched.length, foldedCount };
}

/** Search displayed work after any shell assignment, while retaining its real
 * workspace roots. A project's label alone cannot distinguish same-name roots. */
export function matchesWayfinderWork(item: WayfinderWorkItem, query: string, projectRoots: readonly string[] = []): boolean {
  const needle = query.trim().toLocaleLowerCase();
  return !needle || [item.title, item.project, ...item.workspaceRoots, ...projectRoots, ...item.agents]
    .join('\n').toLocaleLowerCase().includes(needle);
}

/**
 * Runtime activity is intentionally not the desktop's lossy recent-work view.
 * It keeps every busy standalone Session, folds busy Room participants into
 * their canonical Room, and never treats Room `active` (which means merely
 * "not archived") as execution evidence.
 */
export function projectRunningWayfinderWork({
  roomStatusFresh = true,
  rooms,
  sessionStatusFresh = true,
  sessions,
}: {
  nowMs: number;
  roomStatusFresh?: boolean;
  rooms: readonly WayfinderWorkRoomSource[];
  sessionStatusFresh?: boolean;
  sessions: readonly WayfinderWorkSessionSource[];
}): WayfinderWorkItem[] {
  if (!sessionStatusFresh) return [];
  const liveSessions = sessions.filter((session) => session.status !== 'archived');
  const partnerSessions = liveSessions.filter((session) => session.roomParticipant?.roomId);
  const plainSessions = liveSessions.filter((session) => !session.roomParticipant?.roomId);
  const runningSessions = plainSessions
    .filter((session) => session.status === 'busy')
    .map((session) => sessionRow(session, true));
  if (!roomStatusFresh) return runningSessions.sort((left, right) => right.updatedAtMs - left.updatedAtMs);
  const runningRoomIds = busyRoomIds(partnerSessions, true);
  const runningRooms = rooms
    .filter((room) => room.status !== 'archived' && runningRoomIds.has(room.id))
    .map((room) => roomRow(room, { recordFresh: true, runtimeFresh: true, running: true }));
  return [...runningSessions, ...runningRooms].sort((left, right) => right.updatedAtMs - left.updatedAtMs);
}

function sessionRow(session: WayfinderWorkSessionSource, fresh: boolean): WayfinderWorkItem {
  const roots = normalizedWorkspaceRoots(session.workspaceRoots);
  const activity: WayfinderWorkActivity = !fresh
    ? 'unknown'
    : session.status === 'busy'
      ? 'running'
      : session.status === 'faulted'
        ? 'attention'
        : 'idle';
  return {
    key: `session:${session.id}`,
    projectKey: projectKey(session.workspaceRoots),
    workspaceRoots: roots,
    kind: 'session',
    id: session.id,
    title: session.title || '未命名工作',
    project: projectLeaf(session.workspaceRoots),
    updatedAtMs: session.updatedAtMs,
    activity,
    runtimeRunning: fresh && session.status === 'busy',
    statusLabel: activity === 'unknown' ? '已离线' : activity === 'running' ? '进行中' : activity === 'attention' ? '需要处理' : '就绪',
    detail: activity === 'unknown'
      ? '同步中断，显示最近记录'
      : session.status === 'busy'
      ? publicPreview(session.lastMessagePreview, '当前公开内容') || '当前进度不可用'
      : session.status === 'faulted'
        ? publicPreview(session.lastMessagePreview, '最近公开内容') || '故障原因不可用'
        : publicPreview(session.lastMessagePreview, '最近公开内容') || '暂无公开进度',
    agents: [],
    repeats: [],
  };
}

function roomRow(room: WayfinderWorkRoomSource, status: {
  recordFresh: boolean;
  runtimeFresh: boolean;
  running: boolean;
}): WayfinderWorkItem {
  const roots = normalizedWorkspaceRoots(room.workspaceRoots);
  const attention = status.recordFresh && (room.workItems?.some((item) => item.state === 'blocked') ?? false);
  const activity: WayfinderWorkActivity = !status.recordFresh
    ? 'unknown'
    : attention
      ? 'attention'
      : !status.runtimeFresh
        ? 'unknown'
        : status.running
          ? 'running'
          : 'idle';
  return {
    key: `room:${room.id}`,
    projectKey: projectKey(room.workspaceRoots),
    workspaceRoots: roots,
    kind: 'room',
    id: room.id,
    title: room.title || '未命名工作',
    project: projectLeaf(room.workspaceRoots),
    updatedAtMs: room.updatedAtMs,
    activity,
    runtimeRunning: status.recordFresh && status.runtimeFresh && status.running,
    statusLabel: activity === 'unknown' ? '已离线' : activity === 'attention' ? '需要处理' : activity === 'running' ? '进行中' : '就绪',
    detail: activity === 'unknown' ? '同步中断，显示最近记录' : roomWorkDetail(room.workItems),
    ...(status.recordFresh && room.workItems?.length ? {
      progress: { completed: room.workItems.filter((item) => item.state === 'done').length, total: room.workItems.length },
    } : {}),
    agents: (room.participants ?? [])
      .filter((participant) => participant.status !== 'removed')
      .sort((left, right) => (left.ordinal ?? 0) - (right.ordinal ?? 0))
      .map((participant) => roomPlanetName(participant.ordinal)),
    repeats: [],
  };
}

function busyRoomIds(sessions: readonly WayfinderWorkSessionSource[], fresh: boolean): Set<string> {
  if (!fresh) return new Set();
  return new Set(sessions.flatMap((session) => (
    session.status === 'busy' && session.roomParticipant?.roomId
      ? [session.roomParticipant.roomId]
      : []
  )));
}

/* Partner Sessions without a Room row remain folded out; a synthetic Room
   cannot satisfy the canonical-object click contract. */
function normalizedTitle(title: string): string {
  return title.replace(/\s+/g, ' ').replace(/[…]+$/, '').trim().toLocaleLowerCase();
}

function projectLeaf(roots: readonly string[] | undefined): string {
  const first = projectWorkspaceRoots(roots)[0] ?? '';
  if (!first) return '';
  return first === '/' ? '文件系统根目录' : first.split('/').filter(Boolean).at(-1) ?? '';
}

function projectKey(roots: readonly string[] | undefined): string {
  const normalized = projectWorkspaceRoots(roots);
  return normalized.length ? normalized.join('\u001f') : '__unbound__';
}

/** Root access is a capability, not another project when a concrete root exists.
 * Keep the original roots on each item for Files and Runtime consumers. */
function projectWorkspaceRoots(roots: readonly string[] | undefined): string[] {
  const all = normalizedWorkspaceRoots(roots);
  const concrete = all.filter((root) => root !== '/');
  return concrete.length ? concrete : all;
}

function normalizedWorkspaceRoots(roots: readonly string[] | undefined): string[] {
  return [...new Set((roots ?? []).map((root) => root.trim()).filter(Boolean))].sort();
}

/**
 * Rebuild the recency buckets after the desktop moves a dialogue file.  The
 * source project buckets cannot be reused in that case: their item lists are
 * keyed to the old workspace project, so a moved file would be counted in the
 * destination project but disappear from its opened folder.  Keeping this
 * projection helper here gives both the initial and reassigned views the same
 * bucket semantics.
 */
export function bucketizeWayfinderWork(items: readonly WayfinderWorkItem[], nowMs: number): WayfinderWorkBucket[] {
  return (['today', 'week', 'earlier'] as const)
    .map((id) => ({ id, label: BUCKET_LABELS[id], items: items.filter((item) => bucketFor(item.updatedAtMs, nowMs) === id), previewCount: BUCKET_PREVIEW[id] }))
    .filter((bucket) => bucket.items.length > 0);
}

function publicPreview(value: unknown, label: string): string {
  if (typeof value !== 'string') return '';
  const normalized = value.replace(/\s+/gu, ' ').trim();
  if (!normalized) return '';
  const bounded = normalized.length <= 96 ? normalized : `${normalized.slice(0, 95).trimEnd()}…`;
  return `${label}：${bounded}`;
}

function roomWorkDetail(workItems: WayfinderWorkRoomSource['workItems']): string {
  if (!workItems) return '任务进度不可用';
  const focus = [...workItems].sort((left, right) => workPriority(left.state) - workPriority(right.state) || right.updatedAtMs - left.updatedAtMs)[0];
  if (!focus) return '尚无任务';
  const text = focus.state === 'blocked'
    ? focus.blocker?.reason?.trim()
    : focus.state === 'active' || focus.state === 'review' || focus.state === 'queued'
      ? focus.objective?.trim()
      : focus.resultSummary?.trim();
  const bounded = text ? (text.length <= 96 ? text : `${text.slice(0, 95).trimEnd()}…`) : '';
  if (focus.state === 'blocked') return bounded ? `阻塞：${bounded}` : '阻塞原因不可用';
  if (focus.state === 'active') return bounded ? `当前任务：${bounded}` : '当前进度不可用';
  if (focus.state === 'review') return bounded ? `待复核：${bounded}` : '复核内容不可用';
  if (focus.state === 'queued') return bounded ? `待开始：${bounded}` : '待开始任务内容不可用';
  if (focus.state === 'failed') return bounded ? `失败结果：${bounded}` : '失败原因不可用';
  if (focus.state === 'done') return bounded ? `最近结果：${bounded}` : '结果摘要不可用';
  return bounded ? `已取消：${bounded}` : '已取消任务内容不可用';
}

function workPriority(state: NonNullable<WayfinderWorkRoomSource['workItems']>[number]['state']): number {
  return { blocked: 0, active: 1, review: 2, queued: 3, failed: 4, done: 5, cancelled: 6 }[state];
}

function bucketFor(updatedAtMs: number, nowMs: number): WayfinderWorkBucketId {
  const updated = new Date(updatedAtMs);
  const now = new Date(nowMs);
  if (
    updated.getFullYear() === now.getFullYear()
    && updated.getMonth() === now.getMonth()
    && updated.getDate() === now.getDate()
  ) return 'today';
  return nowMs - updatedAtMs < 7 * 86_400_000 ? 'week' : 'earlier';
}

export function wayfinderWorkTime(updatedAtMs: number, nowMs: number): string {
  const delta = Math.max(0, nowMs - updatedAtMs);
  if (delta < 60_000) return '刚刚';
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)} 分钟前`;
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)} 小时前`;
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric' }).format(new Date(updatedAtMs));
}
