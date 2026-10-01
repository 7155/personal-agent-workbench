import { Bot, BrainCircuit, Radio, Users } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { publishGlobalNotice } from '@/components/feedback';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/primitives';
import {
  isModelQuotaError,
  publicAgentErrorText,
} from '@/features/agent/public-error';
import { usePawDesktopApi } from '../runtime/desktop-context';
import { projectRunningWayfinderWork, type WayfinderWorkItem } from './wayfinder-work-projection';
import { usePawWorkDirectory, type PawMemoryMaintenanceActivity } from './PawWorkDirectory';
import './paw-shell-status.css';

export function PawBackgroundActivity() {
  const api = usePawDesktopApi();
  const {
    maintenance,
    maintenanceJob,
    maintenanceStatusFresh,
    roomStatusFresh,
    rooms,
    sessionStatusFresh,
    sessions,
  } = usePawWorkDirectory();
  const [open, setOpen] = useState(false);
  const running = useMemo<BackgroundActivityItem[]>(() => [
    ...projectRunningWayfinderWork({ nowMs: Date.now(), roomStatusFresh, rooms, sessionStatusFresh, sessions }),
    ...(maintenance ? [{ ...maintenance, key: `maintenance:${maintenance.id}`, kind: 'maintenance' as const }] : []),
  ].sort((left, right) => right.updatedAtMs - left.updatedAtMs), [maintenance, roomStatusFresh, rooms, sessionStatusFresh, sessions]);
  useRuntimeCompletionNotices({
    maintenanceStatusFresh,
    maintenanceJob,
    roomStatusFresh,
    rooms,
    running,
    sessionStatusFresh,
    sessions,
  });

  if (!running.length) return null;

  const openWork = (item: BackgroundActivityItem) => {
    if (item.kind === 'maintenance') {
      api.getState().openApp('memory', { initialRoute: '/memory?view=organize', title: 'Memory' });
      setOpen(false);
      return;
    }
    api.getState().openApp('agent', {
      entityId: item.id,
      initialRoute: `/agent?${item.kind === 'room' ? 'room' : 'session'}=${encodeURIComponent(item.id)}`,
      target: { kind: item.kind, id: item.id, title: item.title },
      title: item.title,
    });
    setOpen(false);
  };

  return (
    <Popover onOpenChange={setOpen} open={open}>
      <PopoverTrigger asChild>
        <button
          aria-label={`${running.length} 个后台工作正在运行`}
          className="paw-background-activity__trigger"
          type="button"
        >
          <Radio aria-hidden="true" size={14} />
          <span className="paw-background-activity__trigger-copy">{activityCategory(running[0]!)}</span>
          <b>{running.length}</b>
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="paw-background-activity__popover" sideOffset={8}>
        <section aria-label="后台运行" className="paw-background-activity">
          <header>
            <span><Radio aria-hidden="true" size={15} /><strong>后台运行</strong></span>
            <small>{running.length} 项</small>
          </header>
          <div className="paw-background-activity__list">
            {running.map((item) => {
              const CategoryIcon = item.kind === 'room'
                ? Users
                : item.kind === 'maintenance'
                  ? BrainCircuit
                  : Bot;
              return (
                <button key={item.key} onClick={() => openWork(item)} type="button">
                  <span className="paw-background-activity__icon"><CategoryIcon aria-hidden="true" size={15} /></span>
                  <span>
                    <small>{activityCategory(item)}</small>
                    <strong>{item.title}</strong>
                    <span>{item.detail}</span>
                  </span>
                  <i aria-hidden="true" />
                </button>
              );
            })}
          </div>
          <p>点击可回到对应的对话或 Room。</p>
        </section>
      </PopoverContent>
    </Popover>
  );
}

type BackgroundActivityItem = WayfinderWorkItem | (PawMemoryMaintenanceActivity & {
  key: string;
  kind: 'maintenance';
});

function activityCategory(item: BackgroundActivityItem): string {
  if (item.kind === 'room') return 'Room 协作';
  return item.kind === 'maintenance' ? '自动记忆整理' : '对话 Agent';
}

function useRuntimeCompletionNotices({
  maintenanceJob,
  maintenanceStatusFresh,
  roomStatusFresh,
  rooms,
  running,
  sessionStatusFresh,
  sessions,
}: {
  maintenanceJob: ReturnType<typeof usePawWorkDirectory>['maintenanceJob'];
  maintenanceStatusFresh: boolean;
  roomStatusFresh: boolean;
  rooms: ReturnType<typeof usePawWorkDirectory>['rooms'];
  running: readonly BackgroundActivityItem[];
  sessionStatusFresh: boolean;
  sessions: ReturnType<typeof usePawWorkDirectory>['sessions'];
}) {
  const previousRef = useRef<Map<string, BackgroundActivityItem> | null>(null);
  const previousSessionsRef = useRef<typeof sessions | null>(null);
  useEffect(() => {
    const current = new Map(running.map((item) => [item.key, item]));
    const previous = previousRef.current;
    if (!previous) {
      previousRef.current = current;
      previousSessionsRef.current = sessions;
      return;
    }
    const previousSessions = previousSessionsRef.current ?? [];
    const nextPrevious = new Map(current);
    for (const [key, item] of previous) {
      if (current.has(key)) continue;
      const fresh = item.kind === 'maintenance'
        ? maintenanceStatusFresh
        : item.kind === 'room'
          ? roomStatusFresh && sessionStatusFresh
          : sessionStatusFresh;
      if (!fresh) {
        nextPrevious.set(key, item);
        continue;
      }
      const session = item.kind === 'session' ? sessions.find((candidate) => candidate.id === item.id) : undefined;
      const room = item.kind === 'room' ? rooms.find((candidate) => candidate.id === item.id) : undefined;
      /* A missing row can mean pagination or archival rather than completion;
         only a still-present canonical object may close a live notification. */
      if (item.kind === 'maintenance' && maintenanceJob?.id !== item.id) continue;
      if (item.kind !== 'maintenance' && !session && !room) continue;
      const terminalPartner = item.kind === 'room' ? sessions.find((candidate) => (
        candidate.roomParticipant?.roomId === item.id
        && candidate.status !== 'busy'
        && previousSessions.some((previousSession) => (
          previousSession.id === candidate.id && previousSession.status === 'busy'
        ))
      )) : undefined;
      const failedPartner = item.kind === 'room' && sessions.some((candidate) => (
        candidate.roomParticipant?.roomId === item.id && candidate.status === 'faulted'
      ));
      const needsAttention = session?.status === 'faulted'
        || failedPartner
        || room?.workItems?.some((workItem) => workItem.state === 'blocked')
        || (item.kind === 'maintenance' && (maintenanceJob?.state === 'failed' || maintenanceJob?.state === 'expired'));
      if (!needsAttention && room?.workItems?.some((workItem) => (
        workItem.state === 'queued' || workItem.state === 'active' || workItem.state === 'review'
      ))) {
        // A Partner becoming idle is an ordinary handoff, not a terminal Room.
        // Keep observing until the canonical responsibilities settle, without
        // inventing a running Pi Session during the scheduler's idle gap.
        nextPrevious.set(key, item);
        continue;
      }
      publishGlobalNotice({
        id: runtimeCompletionNoticeId({ item, maintenanceJob, room, session, terminalPartner }),
        title: needsAttention ? `${item.title} 需要处理` : `${item.title} 已结束运行`,
        message: item.kind === 'maintenance'
          ? needsAttention
            ? `自动记忆整理失败，可在 Memory 中检查并重试。${maintenanceJob?.error ? ` ${maintenanceFailureDetail(maintenanceJob.error)}` : ''}`
            : '自动记忆整理已完成，可在 Memory 中查看最新状态。'
          : item.kind === 'room'
            ? failedPartner
              ? 'Room 的 Partner 运行失败，可从项目桌面重新打开并检查详情。'
              : room?.workItems?.length
                ? '本轮协作已结束，可打开 Room 查看成果和未完成项。'
                : 'Room 当前没有正在执行的 Partner，可从项目桌面重新打开。'
            : '对话 Agent 已退出运行态，可从项目桌面重新打开。',
        tone: needsAttention ? 'warning' : 'info',
      });
    }
    previousRef.current = nextPrevious;
    if (sessionStatusFresh) previousSessionsRef.current = sessions;
  }, [maintenanceJob, maintenanceStatusFresh, roomStatusFresh, rooms, running, sessionStatusFresh, sessions]);
}

export function maintenanceFailureDetail(error: string): string {
  // Keep ordinary provider diagnostics available to the user, but collapse
  // the old raw bootstrap-budget exception into the same non-blocking copy as
  // foreground Agent errors. The full error remains in the Runtime receipt.
  if (isModelQuotaError(error)) {
    return '本轮记忆整理因模型服务额度暂时用尽而跳过，不影响对话；稍后可重试或切换已配置模型。';
  }
  return publicAgentErrorText(error, error);
}

/**
 * A poll is transport activity, not a new user event. Use the Runtime's stable
 * causal identity so later metadata updates replace the same notice. Titles,
 * summaries and timestamps are mutable presentation data, never identity.
 */
function runtimeCompletionNoticeId({
  item,
  maintenanceJob,
  room,
  session,
  terminalPartner,
}: {
  item: BackgroundActivityItem;
  maintenanceJob: ReturnType<typeof usePawWorkDirectory>['maintenanceJob'];
  room: ReturnType<typeof usePawWorkDirectory>['rooms'][number] | undefined;
  session: ReturnType<typeof usePawWorkDirectory>['sessions'][number] | undefined;
  terminalPartner: ReturnType<typeof usePawWorkDirectory>['sessions'][number] | undefined;
}): string {
  if (item.kind === 'maintenance') {
    const job = maintenanceJob?.id === item.id ? maintenanceJob : undefined;
    return `runtime-transition:maintenance:${job?.id || item.id}:${job?.state || 'terminal'}`;
  }
  if (item.kind === 'session') {
    const identity = terminalCausalIdentity(session);
    return `runtime-transition:session:${item.id}:${identity || 'unidentified-terminal'}`;
  }
  if (terminalPartner) {
    const identity = terminalCausalIdentity(terminalPartner);
    return `runtime-transition:room:${item.id}:${terminalPartner.id}:${identity || 'unidentified-terminal'}`;
  }
  const roomIdentity = terminalCausalIdentity(room);
  return `runtime-transition:room:${item.id}:${roomIdentity || 'unidentified-terminal'}`;
}

const TERMINAL_CAUSAL_ID_FIELDS = [
  'lastTerminalTurnId',
  'terminalEventId',
  'turnId',
  'rootId',
  'runId',
  'traceId',
] as const;

function terminalCausalIdentity(value: unknown): string {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return '';
  const record = value as Record<string, unknown>;
  for (const field of TERMINAL_CAUSAL_ID_FIELDS) {
    const identity = typeof record[field] === 'string' ? record[field].trim() : '';
    if (identity) return `${field}:${identity}`;
  }
  return '';
}
