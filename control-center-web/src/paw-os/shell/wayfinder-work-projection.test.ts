import { describe, expect, it } from 'vitest';
import {
  projectRunningWayfinderWork,
  projectWayfinderWork,
  wayfinderWorkTime,
  type WayfinderWorkRoomSource,
  type WayfinderWorkSessionSource,
} from './wayfinder-work-projection';

/* The projection is the desktop-density contract: whatever web-length noise
 * the directory returns (partner clones, repeated goal prompts, months of
 * history), the desktop receives a short, folded, bucketed view. These tests
 * pin the folding rules with the exact shapes the user's screenshot showed —
 * "迁移作战室 · Agent 1/2/3" clones and many identical goal rows. */

// Local-time anchors: bucketFor compares calendar days in the runner's zone.
const NOW = new Date(2026, 7, 25, 12, 0, 0).getTime();
const TODAY_9AM = new Date(2026, 7, 25, 9, 0, 0).getTime();
const TWO_DAYS_AGO = NOW - 2 * 86_400_000;
const NINE_DAYS_AGO = NOW - 9 * 86_400_000;

function session(overrides: Partial<WayfinderWorkSessionSource> & { id: string }): WayfinderWorkSessionSource {
  return {
    title: '未命名工作',
    status: 'idle',
    updatedAtMs: TODAY_9AM,
    ...overrides,
  };
}

function room(overrides: Partial<WayfinderWorkRoomSource> & { id: string }): WayfinderWorkRoomSource {
  return {
    title: '未命名工作',
    status: 'idle',
    updatedAtMs: TODAY_9AM,
    ...overrides,
  };
}

describe('projectWayfinderWork', () => {
  it('searches full workspace paths without conflating duplicate project leaves', () => {
    const sources = {
      nowMs: NOW, rooms: [],
      sessions: [
        session({ id: 'first', title: '继续设计', workspaceRoots: ['/work/client-a/workspace'] }),
        session({ id: 'second', title: '继续设计', workspaceRoots: ['/work/client-b/workspace'] }),
      ],
    };
    expect(projectWayfinderWork(sources).projects).toHaveLength(2);
    expect(projectWayfinderWork({ ...sources, query: '/work/client-b' }).projects).toMatchObject([
      { id: '/work/client-b/workspace', label: 'workspace', items: [{ id: 'second' }] },
    ]);
  });

  it('reports only recorded fresh Room task counts and never infers Session percentages', () => {
    const input = {
      nowMs: NOW,
      rooms: [room({ id: 'room-progress', workItems: [
        { state: 'done', updatedAtMs: NOW },
        { state: 'active', updatedAtMs: NOW },
        { state: 'blocked', updatedAtMs: NOW },
      ] })],
      sessions: [session({ id: 'session-progress', status: 'busy' })],
    };
    const rows = projectWayfinderWork(input).projects.flatMap((project) => project.items);
    expect(rows.find((item) => item.id === 'room-progress')?.progress).toEqual({ completed: 1, total: 3 });
    expect(rows.find((item) => item.id === 'session-progress')?.progress).toBeUndefined();
    const stale = projectWayfinderWork({ ...input, roomStatusFresh: false }).projects.flatMap((project) => project.items);
    expect(stale.find((item) => item.id === 'room-progress')?.progress).toBeUndefined();
  });
  it('does not synthesize a Room row when the canonical Room record is absent', () => {
    const view = projectWayfinderWork({
      nowMs: NOW,
      rooms: [],
      sessions: [
        session({ id: 's-1', title: '迁移作战室 · Agent 1', roomParticipant: { roomId: 'room-9' } }),
        session({ id: 's-2', title: '迁移作战室 · Agent 2', status: 'busy', roomParticipant: { roomId: 'room-9' }, updatedAtMs: TODAY_9AM + 60_000 }),
        session({ id: 's-3', title: '迁移作战室 · Agent 3', roomParticipant: { roomId: 'room-9' } }),
      ],
    });

    expect(view.rowCount).toBe(0);
    expect(view.foldedCount).toBe(3);
    expect(view.projects).toEqual([]);
  });

  it('keeps partner Sessions off the list and does not treat an unarchived Room as running', () => {
    const view = projectWayfinderWork({
      nowMs: NOW,
      rooms: [room({
        id: 'room-1',
        title: '迁移作战室',
        status: 'active',
        participants: [
          { displayName: 'Agent 2', ordinal: 2 },
          { displayName: 'Agent 1', ordinal: 1 },
          { displayName: '已移除', ordinal: 3, status: 'removed' },
        ],
      })],
      sessions: [
        session({ id: 's-1', title: '迁移作战室 · Agent 1', roomParticipant: { roomId: 'room-1' } }),
        session({ id: 's-2', title: '迁移作战室 · Agent 2', roomParticipant: { roomId: 'room-1' } }),
        session({ id: 's-3', title: '独立调查', updatedAtMs: TODAY_9AM + 1 }),
      ],
    });

    expect(view.rowCount).toBe(2);
    expect(view.foldedCount).toBe(2);
    const titles = view.buckets[0]!.items.map((item) => item.title);
    expect(titles).toEqual(['独立调查', '迁移作战室']);
    const roomItem = view.buckets[0]!.items.find((item) => item.kind === 'room');
    expect(roomItem?.agents).toEqual(['Mars', 'Venus']);
    expect(roomItem?.activity).toBe('idle');
  });

  it('projects a Room as running only while one of its real participant Sessions is busy', () => {
    const sources = {
      nowMs: NOW,
      rooms: [room({ id: 'room-1', title: '迁移作战室', status: 'active' })],
      sessions: [
        session({ id: 'partner-1', title: '迁移作战室 · Agent 1', status: 'busy', roomParticipant: { roomId: 'room-1' } }),
        session({ id: 'partner-2', title: '迁移作战室 · Agent 2', roomParticipant: { roomId: 'room-1' } }),
      ],
    };

    expect(projectWayfinderWork(sources).projects[0]?.items[0]).toMatchObject({
      id: 'room-1',
      activity: 'running',
      statusLabel: '进行中',
    });
    expect(projectRunningWayfinderWork(sources)).toEqual([
      expect.objectContaining({ id: 'room-1', kind: 'room', activity: 'running' }),
    ]);
  });

  it('keeps the running signal when a busy Room also has a blocked WorkItem', () => {
    const sources = {
      nowMs: NOW,
      rooms: [room({
        id: 'room-mixed',
        title: '并行修复',
        status: 'active',
        workItems: [{ state: 'blocked' as const, updatedAtMs: NOW, blocker: { reason: '等待确认' } }],
      })],
      sessions: [session({
        id: 'partner-mixed',
        title: '并行修复 · Agent 1',
        status: 'busy',
        roomParticipant: { roomId: 'room-mixed' },
      })],
    };

    expect(projectWayfinderWork(sources).projects[0]).toMatchObject({
      attentionCount: 1,
      runningCount: 1,
      items: [expect.objectContaining({ activity: 'attention', runtimeRunning: true })],
    });
  });

  it('keeps lossy recent-work folding out of the live activity projection', () => {
    const running = projectRunningWayfinderWork({
      nowMs: NOW,
      rooms: [],
      sessions: [
        session({ id: 'busy-a', title: '同名任务', status: 'busy', updatedAtMs: TODAY_9AM + 2 }),
        session({ id: 'busy-b', title: '同名任务', status: 'busy', updatedAtMs: TODAY_9AM + 1 }),
      ],
    });

    expect(running.map((item) => item.id)).toEqual(['busy-a', 'busy-b']);
  });

  it('folds only quiet repeated goals and keeps a current fault visible as its own record', () => {
    const view = projectWayfinderWork({
      nowMs: NOW,
      rooms: [],
      sessions: [
        session({ id: 's-old', title: '重构 Wayfinder 列表', status: 'faulted', updatedAtMs: TODAY_9AM }),
        session({ id: 's-new', title: '重构  Wayfinder 列表…', updatedAtMs: TODAY_9AM + 120_000 }),
        session({ id: 's-mid', title: '重构 Wayfinder 列表', updatedAtMs: TODAY_9AM + 60_000 }),
      ],
    });

    expect(view.rowCount).toBe(2);
    expect(view.foldedCount).toBe(1);
    expect(view.buckets[0]!.items.map((row) => [row.id, row.activity])).toEqual([
      ['s-new', 'idle'],
      ['s-old', 'attention'],
    ]);
    expect(view.buckets[0]!.items[0]!.repeats.map((repeat) => repeat.id)).toEqual(['s-mid']);
  });

  it('does not promote historical terminal Room failures to current attention', () => {
    const view = projectWayfinderWork({
      nowMs: NOW,
      rooms: [room({
        id: 'room-history',
        title: '历史失败 Room',
        status: 'active',
        workItems: [{ state: 'failed', resultSummary: '旧任务失败', updatedAtMs: NOW - 60_000 }],
      })],
      sessions: [],
    });

    expect(view.projects[0]?.items[0]).toMatchObject({ activity: 'idle', statusLabel: '就绪' });
  });

  it('renders unknown provenance instead of stale running or failure states', () => {
    const view = projectWayfinderWork({
      nowMs: NOW,
      roomStatusFresh: false,
      sessionStatusFresh: false,
      rooms: [room({ id: 'room-stale', title: '状态过期 Room', status: 'active' })],
      sessions: [session({ id: 'session-stale', title: '状态过期 Session', status: 'busy' })],
    });

    expect(view.projects.flatMap((project) => project.items).map((item) => [item.statusLabel, item.detail])).toEqual([
      ['已离线', '同步中断，显示最近记录'],
      ['已离线', '同步中断，显示最近记录'],
    ]);
    expect(projectRunningWayfinderWork({
      nowMs: NOW,
      roomStatusFresh: false,
      sessionStatusFresh: false,
      rooms: [room({ id: 'room-stale', title: '状态过期 Room', status: 'active' })],
      sessions: [session({ id: 'session-stale', title: '状态过期 Session', status: 'busy' })],
    })).toEqual([]);
  });

  it('groups rows into 今天 / 本周 / 更早 with a bounded preview per bucket', () => {
    const view = projectWayfinderWork({
      nowMs: NOW,
      rooms: [],
      sessions: [
        ...Array.from({ length: 7 }, (_, index) => session({
          id: `today-${index}`,
          title: `今日目标 ${index}`,
          updatedAtMs: TODAY_9AM + index,
        })),
        session({ id: 'week-1', title: '周中目标', updatedAtMs: TWO_DAYS_AGO }),
        session({ id: 'old-1', title: '陈旧目标 A', updatedAtMs: NINE_DAYS_AGO }),
        session({ id: 'old-2', title: '陈旧目标 B', updatedAtMs: NINE_DAYS_AGO }),
      ],
    });

    expect(view.buckets.map((bucket) => bucket.id)).toEqual(['today', 'week', 'earlier']);
    const [today, week, earlier] = view.buckets;
    expect(today!.items).toHaveLength(7);
    expect(today!.previewCount).toBe(5);
    expect(week!.items).toHaveLength(1);
    // The stale bucket rests fully collapsed: zero rows on the first screen.
    expect(earlier!.previewCount).toBe(0);
    expect(earlier!.items).toHaveLength(2);
  });

  it('drops archived records and empty buckets', () => {
    const view = projectWayfinderWork({
      nowMs: NOW,
      rooms: [room({ id: 'room-a', title: '已归档房间', status: 'archived' })],
      sessions: [
        session({ id: 's-1', title: '仍在进行' }),
        session({ id: 's-2', title: '已归档工作', status: 'archived' }),
      ],
    });

    expect(view.rowCount).toBe(1);
    expect(view.buckets.map((bucket) => bucket.id)).toEqual(['today']);
    expect(view.buckets[0]!.items[0]!.title).toBe('仍在进行');
  });

  it('uses concrete project names and groups access-root variants without losing roots', () => {
    const view = projectWayfinderWork({
      nowMs: NOW, rooms: [],
      sessions: [
        session({ id: 'a', title: 'first', workspaceRoots: ['/work/paw', '/'] }),
        session({ id: 'b', title: 'second', workspaceRoots: ['/work/paw'] }),
        session({ id: 'c', title: 'third', workspaceRoots: ['/', '/work/other'] }),
        session({ id: 'd', title: 'root', workspaceRoots: ['/'] }),
        session({ id: 'e', title: 'chat', workspaceRoots: [] }),
      ],
    });
    expect(view.projects).toHaveLength(4);
    expect(view.projects.find((project) => project.label === 'paw')).toMatchObject({
      sessionCount: 2, workspaceRoots: ['/', '/work/paw'],
    });
    expect(view.projects.map((project) => project.label)).toEqual(expect.arrayContaining([
      'paw', 'other', '文件系统根目录', '未绑定项目',
    ]));
  });

  it('projects workspace roots and current status counts for a project context', () => {
    const view = projectWayfinderWork({
      nowMs: NOW,
      sessions: [
        session({ id: 's-running', title: '运行中对话', status: 'busy', workspaceRoots: ['/work/paw', '/work/shared'] }),
        session({ id: 's-attention', title: '待处理对话', status: 'faulted', workspaceRoots: ['/work/paw', '/work/shared'] }),
      ],
      rooms: [room({
        id: 'room-blocked',
        title: '阻塞协作',
        status: 'active',
        workspaceRoots: ['/work/shared', '/work/paw'],
        workItems: [{ state: 'blocked', blocker: { reason: '等待输入' }, updatedAtMs: NOW }],
      })],
    });

    expect(view.projects).toHaveLength(1);
    expect(view.projects[0]).toMatchObject({
      workspaceRoots: ['/work/paw', '/work/shared'],
      sessionCount: 2,
      roomCount: 1,
      runningCount: 1,
      attentionCount: 2,
    });
  });

  it('searches title, project leaf and partner names without touching the caps', () => {
    const sources = {
      nowMs: NOW,
      rooms: [room({
        id: 'room-1',
        title: '迁移作战室',
        participants: [{ displayName: 'Kimi', ordinal: 1 }],
      })],
      sessions: [
        session({ id: 's-1', title: '重构列表', workspaceRoots: ['/Users/me/paw/control-center-web'] }),
        session({ id: 's-2', title: '其他工作' }),
      ],
    };

    expect(projectWayfinderWork({ ...sources, query: 'control-center' }).rowCount).toBe(1);
    expect(projectWayfinderWork({ ...sources, query: 'mars' }).rowCount).toBe(1);
    expect(projectWayfinderWork({ ...sources, query: 'kimi' }).rowCount).toBe(0);
    expect(projectWayfinderWork({ ...sources, query: '重构' }).rowCount).toBe(1);
    expect(projectWayfinderWork({ ...sources, query: '不存在' }).rowCount).toBe(0);
  });
});

describe('wayfinderWorkTime', () => {
  it('speaks desktop recency instead of raw timestamps', () => {
    expect(wayfinderWorkTime(NOW - 20_000, NOW)).toBe('刚刚');
    expect(wayfinderWorkTime(NOW - 5 * 60_000, NOW)).toBe('5 分钟前');
    expect(wayfinderWorkTime(NOW - 3 * 3_600_000, NOW)).toBe('3 小时前');
    expect(wayfinderWorkTime(NINE_DAYS_AGO, NOW)).toMatch(/8\/16|8月16日/);
  });
});
