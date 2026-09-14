import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { useControlTransport } from '@/app/control-transport';
import { sessionItems, type SessionSummary } from '@/features/agent/types';
import type { WayfinderWorkRoomSource } from './wayfinder-work-projection';

const SESSION_LIMIT = 100;
const ROOM_LIMIT = 100;
const DEFAULT_POLL_INTERVAL_MS = 30_000;
const DEFAULT_MAINTENANCE_POLL_INTERVAL_MS = 30_000;
const DEFAULT_ACTIVE_MAINTENANCE_POLL_INTERVAL_MS = 5_000;
const DEFAULT_INITIAL_POLL_DELAY_MS = 1_200;
const MINIMUM_FRESHNESS_MS = 30_000;

type PawWorkDirectoryValue = {
  failed: boolean;
  loaded: boolean;
  loading: boolean;
  maintenance: PawMemoryMaintenanceActivity | null;
  maintenanceJob: PawMemoryMaintenanceJob | null;
  maintenanceStatusFresh: boolean;
  refresh: () => Promise<void>;
  roomStatusFresh: boolean;
  rooms: WayfinderWorkRoomSource[];
  sessionStatusFresh: boolean;
  sessions: SessionSummary[];
};

export type PawMemoryMaintenanceActivity = {
  detail: string;
  id: string;
  title: string;
  updatedAtMs: number;
};

export type PawMemoryMaintenanceJob = {
  error: string;
  id: string;
  state: string;
  updatedAtMs: number;
};

const PawWorkDirectoryContext = createContext<PawWorkDirectoryValue | null>(null);

/**
 * One shell-owned directory sample feeds both the desktop files and menu-bar
 * activity. Polling begins only while the document is visible, schedules the
 * next read after the prior one settles, and aborts stale work on teardown so
 * the status UI never doubles the Wayfinder's list requests.
 */
export function PawWorkDirectoryProvider({
  children,
  initialPollDelayMs = DEFAULT_INITIAL_POLL_DELAY_MS,
  maintenancePollIntervalMs = DEFAULT_MAINTENANCE_POLL_INTERVAL_MS,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
  runningMaintenancePollIntervalMs = DEFAULT_ACTIVE_MAINTENANCE_POLL_INTERVAL_MS,
}: {
  children: ReactNode;
  initialPollDelayMs?: number;
  maintenancePollIntervalMs?: number;
  pollIntervalMs?: number;
  runningMaintenancePollIntervalMs?: number;
}) {
  const transport = useControlTransport();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [rooms, setRooms] = useState<WayfinderWorkRoomSource[]>([]);
  const [maintenance, setMaintenance] = useState<PawMemoryMaintenanceActivity | null>(null);
  const [maintenanceJob, setMaintenanceJob] = useState<PawMemoryMaintenanceJob | null>(null);
  const [maintenanceStatusFresh, setMaintenanceStatusFresh] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  const [sessionStatusFresh, setSessionStatusFresh] = useState(false);
  const [roomStatusFresh, setRoomStatusFresh] = useState(false);
  const directoryAbortRef = useRef<AbortController | null>(null);
  const directoryScheduleResetRef = useRef<() => void>(() => undefined);
  const maintenanceAbortRef = useRef<AbortController | null>(null);
  const directoryGenerationRef = useRef(0);
  const maintenanceGenerationRef = useRef(0);
  const loadedRef = useRef(false);
  const maintenanceSuccessAtRef = useRef(0);
  const maintenanceRunningRef = useRef(false);
  const maintenanceFreshnessMs = Math.max(MINIMUM_FRESHNESS_MS, maintenancePollIntervalMs * 2);

  const refreshDirectory = useCallback(async () => {
    const generation = ++directoryGenerationRef.current;
    directoryAbortRef.current?.abort();
    const controller = new AbortController();
    directoryAbortRef.current = controller;
    if (!loadedRef.current) setLoading(true);
    const [sessionResult, roomResult] = await Promise.allSettled([
      transport.request({ pathId: 'agent.sessions.list', query: { limit: SESSION_LIMIT, projectionOnly: 1 }, signal: controller.signal }),
      transport.request({ pathId: 'agent.rooms.list', query: { limit: ROOM_LIMIT, projectionOnly: 1 }, signal: controller.signal }),
    ]);
    if (controller.signal.aborted || generation !== directoryGenerationRef.current) return;
    if (sessionResult.status === 'fulfilled') {
      setSessionStatusFresh(true);
      setSessions((current) => sameDirectoryValue(current, sessionItems(sessionResult.value)));
    } else {
      setSessionStatusFresh(false);
    }
    if (roomResult.status === 'fulfilled') {
      setRoomStatusFresh(true);
      setRooms((current) => sameDirectoryValue(current, pawWorkRoomSources(roomResult.value)));
    } else {
      setRoomStatusFresh(false);
    }
    setFailed(sessionResult.status === 'rejected' && roomResult.status === 'rejected' && !loadedRef.current);
    if (!loadedRef.current) {
      loadedRef.current = true;
      setLoaded(true);
      setLoading(false);
    }
  }, [transport]);

  const refreshMaintenance = useCallback(async () => {
    const generation = ++maintenanceGenerationRef.current;
    maintenanceAbortRef.current?.abort();
    const controller = new AbortController();
    maintenanceAbortRef.current = controller;
    try {
      const value = await transport.request({
        pathId: 'agent.memoryMaintenance.run',
        query: { limit: 1, projectionOnly: 1 },
        signal: controller.signal,
      });
      if (controller.signal.aborted || generation !== maintenanceGenerationRef.current) return;
      const nextJob = pawMemoryMaintenanceJob(value);
      const next = maintenanceActivityFromJob(value, nextJob);
      maintenanceSuccessAtRef.current = Date.now();
      maintenanceRunningRef.current = Boolean(next);
      setMaintenanceStatusFresh(true);
      setMaintenance((current) => sameMaintenanceActivity(current, next));
      setMaintenanceJob((current) => sameMaintenanceJob(current, nextJob));
    } catch {
      if (controller.signal.aborted || generation !== maintenanceGenerationRef.current) return;
      if (!maintenanceSuccessAtRef.current || Date.now() - maintenanceSuccessAtRef.current > maintenanceFreshnessMs) {
        maintenanceRunningRef.current = false;
        setMaintenanceStatusFresh(false);
        setMaintenance(null);
        setMaintenanceJob(null);
      }
    }
  }, [maintenanceFreshnessMs, transport]);

  // A Team space switch replaces the transport identity. Clear the old
  // projection immediately and invalidate every in-flight completion so a
  // response from the previous space cannot repaint the new desktop.
  useEffect(() => {
    directoryGenerationRef.current += 1;
    maintenanceGenerationRef.current += 1;
    directoryAbortRef.current?.abort();
    maintenanceAbortRef.current?.abort();
    loadedRef.current = false;
    maintenanceSuccessAtRef.current = 0;
    maintenanceRunningRef.current = false;
    setSessions([]);
    setRooms([]);
    setMaintenance(null);
    setMaintenanceJob(null);
    setSessionStatusFresh(false);
    setRoomStatusFresh(false);
    setMaintenanceStatusFresh(false);
    setFailed(false);
    setLoaded(false);
    setLoading(true);
  }, [transport]);

  const refresh = useCallback(async () => {
    if (document.visibilityState === 'hidden') return;
    /* Explicit user/terminal refresh is immediate, but also restarts the
     * passive window so it cannot be followed milliseconds later by a stale
     * scheduled poll (especially noisy while Runtime is offline). */
    directoryScheduleResetRef.current();
    await Promise.all([refreshDirectory(), refreshMaintenance()]);
  }, [refreshDirectory, refreshMaintenance]);

  useEffect(() => {
    let stopped = false;
    let timer = 0;
    const schedule = () => {
      if (stopped || document.visibilityState === 'hidden') return;
      timer = window.setTimeout(() => { void tick(); }, Math.max(1_000, pollIntervalMs));
    };
    const restartSchedule = () => {
      window.clearTimeout(timer);
      if (!stopped && document.visibilityState !== 'hidden') schedule();
    };
    const tick = async () => {
      await refreshDirectory();
      schedule();
    };
    const onVisibilityChange = () => {
      window.clearTimeout(timer);
      if (document.visibilityState === 'hidden') {
        directoryAbortRef.current?.abort();
        // Polling stops here. Keep directory content for recovery, but do not
        // replay its last busy status when the user later returns to the sky.
        setSessionStatusFresh(false);
        setRoomStatusFresh(false);
        return;
      }
      void tick();
    };
    if (document.visibilityState !== 'hidden') {
      if (initialPollDelayMs > 0) {
        timer = window.setTimeout(() => { void tick(); }, initialPollDelayMs);
      } else {
        void tick();
      }
    }
    directoryScheduleResetRef.current = restartSchedule;
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      directoryAbortRef.current?.abort();
      if (directoryScheduleResetRef.current === restartSchedule) {
        directoryScheduleResetRef.current = () => undefined;
      }
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [initialPollDelayMs, pollIntervalMs, refreshDirectory]);

  useEffect(() => {
    let stopped = false;
    let timer = 0;
    const schedule = () => {
      if (stopped || document.visibilityState === 'hidden') return;
      const delay = maintenanceRunningRef.current
        ? runningMaintenancePollIntervalMs
        : maintenancePollIntervalMs;
      timer = window.setTimeout(() => { void tick(); }, Math.max(1_000, delay));
    };
    const tick = async () => {
      await refreshMaintenance();
      schedule();
    };
    const onVisibilityChange = () => {
      window.clearTimeout(timer);
      if (document.visibilityState === 'hidden') {
        maintenanceAbortRef.current?.abort();
        return;
      }
      void tick();
    };
    if (document.visibilityState !== 'hidden') {
      if (initialPollDelayMs > 0) {
        timer = window.setTimeout(() => { void tick(); }, initialPollDelayMs);
      } else {
        void tick();
      }
    }
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      maintenanceAbortRef.current?.abort();
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [initialPollDelayMs, maintenancePollIntervalMs, refreshMaintenance, runningMaintenancePollIntervalMs]);

  const value = useMemo<PawWorkDirectoryValue>(() => ({
    failed,
    loaded,
    loading,
    maintenance,
    maintenanceJob,
    maintenanceStatusFresh,
    refresh,
    roomStatusFresh,
    rooms,
    sessionStatusFresh,
    sessions,
  }), [failed, loaded, loading, maintenance, maintenanceJob, maintenanceStatusFresh, refresh, roomStatusFresh, rooms, sessionStatusFresh, sessions]);

  return <PawWorkDirectoryContext.Provider value={value}>{children}</PawWorkDirectoryContext.Provider>;
}

function sameDirectoryValue<T>(current: T[], next: T[]): T[] {
  return JSON.stringify(current) === JSON.stringify(next) ? current : next;
}

function sameMaintenanceActivity(
  current: PawMemoryMaintenanceActivity | null,
  next: PawMemoryMaintenanceActivity | null,
): PawMemoryMaintenanceActivity | null {
  if (current === next) return current;
  return JSON.stringify(current) === JSON.stringify(next) ? current : next;
}

function sameMaintenanceJob(
  current: PawMemoryMaintenanceJob | null,
  next: PawMemoryMaintenanceJob | null,
): PawMemoryMaintenanceJob | null {
  if (current === next) return current;
  return JSON.stringify(current) === JSON.stringify(next) ? current : next;
}

export function usePawWorkDirectory(): PawWorkDirectoryValue {
  const value = useContext(PawWorkDirectoryContext);
  if (!value) throw new Error('usePawWorkDirectory must be used inside PawWorkDirectoryProvider');
  return value;
}

export function pawWorkRoomSources(value: unknown): WayfinderWorkRoomSource[] {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return [];
  const envelope = value as Record<string, unknown>;
  const source = Array.isArray(envelope.items) ? envelope.items : Array.isArray(envelope.rooms) ? envelope.rooms : [];
  return source.filter((item): item is WayfinderWorkRoomSource => {
    if (typeof item !== 'object' || item === null) return false;
    const room = item as Record<string, unknown>;
    return typeof room.id === 'string'
      && typeof room.title === 'string'
      && typeof room.updatedAtMs === 'number';
  });
}

export function pawMemoryMaintenanceActivity(value: unknown): PawMemoryMaintenanceActivity | null {
  const job = pawMemoryMaintenanceJob(value);
  return maintenanceActivityFromJob(value, job);
}

export function pawMemoryMaintenanceJob(value: unknown): PawMemoryMaintenanceJob | null {
  if (!isRecord(value)) return null;
  const source = isRecord(value.job) ? value.job : value;
  const jobId = typeof source.jobId === 'string' ? source.jobId.trim() : '';
  if (!jobId) return null;
  const state = typeof source.state === 'string' ? source.state : '';
  return {
    error: typeof source.error === 'string' ? source.error.trim() : '',
    id: jobId,
    state,
    updatedAtMs: typeof source.updatedAtMs === 'number' ? source.updatedAtMs : Date.now(),
  };
}

function maintenanceActivityFromJob(
  value: unknown,
  job: PawMemoryMaintenanceJob | null,
): PawMemoryMaintenanceActivity | null {
  if (!job || (job.state !== 'queued' && job.state !== 'running') || !isRecord(value)) return null;
  const source = isRecord(value.job) ? value.job : value;
  const progress = isRecord(source.progress) ? source.progress : {};
  const phase = typeof progress.phase === 'string' ? progress.phase : '';
  const summary = typeof progress.summary === 'string'
    ? progress.summary
    : typeof source.summary === 'string'
      ? source.summary
      : '';
  return {
    id: job.id,
    title: '自动记忆整理',
    detail: summary || phase || '正在整理受治理记忆',
    updatedAtMs: job.updatedAtMs,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
