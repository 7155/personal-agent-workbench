import { useIsFetching, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useControlTransport } from '@/app/control-transport';
import type { EvalRunV1 } from '@/contracts/generated/eval-run.v1';
import type { EvalScheduleCreateV1 } from '@/contracts/generated/eval-schedule-create.v1';
import type { EvalScheduleListV1 } from '@/contracts/generated/eval-schedule-list.v1';
import type { EvalScheduleRunListV1 } from '@/contracts/generated/eval-schedule-run-list.v1';
import type { EvalSuiteListV1 } from '@/contracts/generated/eval-suite-list.v1';
import type { ObservationEventV1 } from '@/contracts/generated/observation-event.v1';
import type { ObservationSnapshotV1 } from '@/contracts/generated/observation-snapshot.v1';
import type { ObservabilityEvidenceEvalRequestV1 } from '@/contracts/generated/observability-evidence-eval-request.v1';
import type { ObservabilityEvalListV1 } from '@/contracts/generated/observability-eval-list.v1';
import type { ObservabilitySandboxRunListV1 } from '@/contracts/generated/observability-sandbox-run-list.v1';
import type { ObservabilityTraceGetV1 } from '@/contracts/generated/observability-trace-get.v1';
import type { UiObservationEvent } from '@/contracts/ui-events';
import type { ControlQueryValue, JsonValue } from '@/platform/transport';
import type { ControlPathId } from '@/platform/routes';

export interface ObservationFilters {
  sessionId?: string;
  roomId?: string;
  traceId?: string;
  runId?: string;
  category?: ObservationEventV1['category'];
  status?: ObservationEventV1['status'];
}

export interface EvalScheduleCreateInput {
  scheduleId?: string;
  suiteId: string;
  suiteRevision: string;
  recurrenceKind: 'daily' | 'weekly';
  recurrenceInterval: number;
  maxRuns: number;
  nextDueAtMs: number;
}

export interface AiJudgeEvalRequest {
  traceId: string;
  evaluator?: {
    provider: string;
    model: string;
    thinking: string;
    displayName: string;
  };
}

export type SandboxRunList = ObservabilitySandboxRunListV1;

export const observabilityQueryKeys = {
  root: ['observability'] as const,
  snapshot: (filterKey: string) => ['observability', 'snapshot', filterKey] as const,
  trace: (traceId: string) => ['observability', 'trace', traceId] as const,
  evals: (traceId: string) => ['observability', 'evals', traceId] as const,
  evalSchedules: () => ['observability', 'eval-schedules'] as const,
  evalSuites: () => ['observability', 'eval-suites'] as const,
  evalScheduleRuns: (scheduleId: string) => ['observability', 'eval-schedules', scheduleId, 'runs'] as const,
  sandboxRuns: () => ['observability', 'sandbox-runs'] as const,
};

const TRACE_EVENT_INVALIDATION_DEBOUNCE_MS = 100;
const TRACE_BUILDING_POLL_MS = 2_000;
const EVAL_ACTIVE_POLL_MS = 2_000;
const SCHEDULE_ACTIVE_POLL_MS = 2_000;
const SCHEDULE_WAITING_POLL_MS = 30_000;
const SANDBOX_RUN_ACTIVE_POLL_MS = 2_000;

const SANDBOX_RUNS_PATH_ID: ControlPathId = 'observability.sandboxRuns.list';

export type ObservationConnectionState =
  | 'connecting'
  | 'live'
  | 'reconnecting'
  | 'offline';

export function useObservationFeed(filters: ObservationFilters) {
  const transport = useControlTransport();
  const queryClient = useQueryClient();
  const [liveItems, setLiveItems] = useState<ObservationEventV1[]>([]);
  const [connection, setConnection] = useState<ObservationConnectionState>('connecting');
  const [streamError, setStreamError] = useState('');
  const filterKey = useMemo(() => JSON.stringify(filters), [filters]);
  const activeSubscription = useRef('');
  const pendingTraceInvalidations = useRef(new Set<string>());
  const traceInvalidationTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const queueTraceInvalidation = useCallback((traceId: string) => {
    if (!traceId) return;
    pendingTraceInvalidations.current.add(traceId);
    if (traceInvalidationTimer.current !== null) return;
    traceInvalidationTimer.current = setTimeout(() => {
      traceInvalidationTimer.current = null;
      const traceIds = new Set(pendingTraceInvalidations.current);
      pendingTraceInvalidations.current.clear();
      void queryClient.invalidateQueries({
        predicate: (query) => {
          const [root, kind, id] = query.queryKey;
          return root === observabilityQueryKeys.root[0]
            && (kind === 'trace' || kind === 'evals')
            && typeof id === 'string'
            && traceIds.has(id);
        },
        refetchType: 'active',
      });
    }, TRACE_EVENT_INVALIDATION_DEBOUNCE_MS);
  }, [queryClient]);
  const snapshot = useQuery({
    queryKey: observabilityQueryKeys.snapshot(filterKey),
    queryFn: ({ signal }) => transport.request<ObservationSnapshotV1>({
      pathId: 'observability.snapshot',
      query: { limit: 300, ...queryFilters(filters) },
      responseContract: 'observation-snapshot.v1',
      signal,
    }),
    retry: false,
    refetchOnWindowFocus: false,
  });

  useEffect(() => {
    setLiveItems([]);
    setConnection('connecting');
    setStreamError('');
  }, [filterKey]);

  useEffect(() => {
    const cursor = snapshot.data?.resumeToken;
    if (!cursor) return;
    const subscriptionKey = `${filterKey}:${cursor}`;
    activeSubscription.current = subscriptionKey;
    const unsubscribe = transport.subscribe<UiObservationEvent>(
      {
        pathId: 'observability.events',
        query: queryFilters(filters),
        lastEventId: cursor,
      },
      {
        open: () => {
          if (activeSubscription.current !== subscriptionKey) return;
          setConnection('live');
          setStreamError('');
        },
        next: (event) => {
          if (
            activeSubscription.current !== subscriptionKey
            || event.eventType === 'snapshot_required'
          ) return;
          setLiveItems((current) => mergeObservationEvents(current, [event], 400));
          queueTraceInvalidation(event.traceId);
        },
        reconnect: () => {
          if (activeSubscription.current !== subscriptionKey) return;
          setConnection('reconnecting');
        },
        error: () => {
          if (activeSubscription.current !== subscriptionKey) return;
          setConnection('offline');
          setStreamError('实时事件暂时不可用，正在保留当前快照并尝试重连。');
        },
        snapshotRequired: () => {
          if (activeSubscription.current !== subscriptionKey) return;
          setLiveItems([]);
          void snapshot.refetch();
        },
      },
    );
    return () => {
      if (activeSubscription.current === subscriptionKey) {
        activeSubscription.current = '';
      }
      unsubscribe();
    };
  }, [filterKey, filters, queueTraceInvalidation, snapshot.data?.resumeToken, transport]);

  useEffect(() => () => {
    if (traceInvalidationTimer.current !== null) {
      clearTimeout(traceInvalidationTimer.current);
      traceInvalidationTimer.current = null;
    }
    pendingTraceInvalidations.current.clear();
  }, []);

  const items = useMemo(
    () => mergeObservationEvents(snapshot.data?.items ?? [], liveItems, 500),
    [liveItems, snapshot.data?.items],
  );

  const refresh = useCallback(async () => {
    await queryClient.invalidateQueries({
      queryKey: observabilityQueryKeys.root,
      refetchType: 'active',
    });
  }, [queryClient]);
  const observabilityFetches = useIsFetching({ queryKey: observabilityQueryKeys.root });

  return {
    connection,
    error: snapshot.error as Error | null,
    isFetching: observabilityFetches > 0,
    isPending: snapshot.isPending,
    items,
    refresh,
    snapshot: snapshot.data,
    streamError,
  };
}

export function useObservationTrace(traceId: string) {
  const transport = useControlTransport();
  return useQuery({
    queryKey: observabilityQueryKeys.trace(traceId),
    enabled: Boolean(traceId),
    queryFn: ({ signal }) => transport.request<ObservabilityTraceGetV1>({
      pathId: 'observability.trace.get',
      params: { traceId },
      query: { limit: 500 },
      responseContract: 'observability-trace-get.v1',
      signal,
    }),
    retry: false,
    refetchOnWindowFocus: false,
    refetchInterval: (query) => query.state.data?.trace.status === 'building'
      ? TRACE_BUILDING_POLL_MS
      : false,
    refetchIntervalInBackground: false,
  });
}

export function useObservationEvals(traceId: string) {
  const transport = useControlTransport();
  return useQuery({
    queryKey: observabilityQueryKeys.evals(traceId),
    enabled: Boolean(traceId),
    queryFn: ({ signal }) => transport.request<ObservabilityEvalListV1>({
      pathId: 'observability.evals.list',
      query: { traceId, limit: 100 },
      responseContract: 'observability-eval-list.v1',
      signal,
    }),
    retry: false,
    refetchOnWindowFocus: false,
    refetchInterval: (query) => hasActiveEval(query.state.data) ? EVAL_ACTIVE_POLL_MS : false,
    refetchIntervalInBackground: false,
  });
}

export function useSandboxRuns() {
  const transport = useControlTransport();
  return useQuery({
    queryKey: observabilityQueryKeys.sandboxRuns(),
    queryFn: ({ signal }) => transport.request<SandboxRunList>({
      pathId: SANDBOX_RUNS_PATH_ID,
      query: { limit: 20 },
      signal,
    }),
    retry: false,
    refetchOnWindowFocus: false,
    refetchInterval: (query) => hasActiveSandboxRun(query.state.data) ? SANDBOX_RUN_ACTIVE_POLL_MS : false,
    refetchIntervalInBackground: false,
  });
}

export function useObservationEvidenceEval() {
  const transport = useControlTransport();
  return useMutation({
    mutationKey: ['observability', 'evals', 'evidence'],
    mutationFn: (body: ObservabilityEvidenceEvalRequestV1) => transport.request<EvalRunV1>({
      pathId: 'observability.evals.evidence.run',
      body: body as unknown as JsonValue,
      responseContract: 'eval-run.v1',
    }),
  });
}

export function useObservationAiJudge() {
  const transport = useControlTransport();
  return useMutation({
    mutationKey: ['observability', 'evals', 'ai-judge'],
    mutationFn: (body: AiJudgeEvalRequest) => transport.request<EvalRunV1>({
      pathId: 'observability.evals.aiJudge.run',
      body: body as unknown as JsonValue,
      responseContract: 'eval-run.v1',
    }),
  });
}

export function useEvalSchedules() {
  const transport = useControlTransport();
  return useQuery({
    queryKey: observabilityQueryKeys.evalSchedules(),
    queryFn: ({ signal }) => transport.request<EvalScheduleListV1>({
      pathId: 'observability.evalSchedules.list',
      query: { limit: 100 },
      responseContract: 'eval-schedule-list.v1',
      signal,
    }),
    retry: false,
    refetchOnWindowFocus: false,
    refetchInterval: (query) => scheduleRefreshInterval(query.state.data),
    refetchIntervalInBackground: false,
  });
}

export function useEvalSuites() {
  const transport = useControlTransport();
  return useQuery({
    queryKey: observabilityQueryKeys.evalSuites(),
    queryFn: ({ signal }) => transport.request<EvalSuiteListV1>({
      pathId: 'observability.evalSuites.list',
      query: { limit: 100 },
      responseContract: 'eval-suite-list.v1',
      signal,
    }),
    retry: false,
    refetchOnWindowFocus: false,
  });
}

export function useCreateEvalSchedule() {
  const transport = useControlTransport();
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: ['observability', 'eval-schedules', 'create'],
    mutationFn: (body: EvalScheduleCreateInput) => transport.request<EvalScheduleCreateV1>({
      pathId: 'observability.evalSchedules.create',
      body: body as unknown as JsonValue,
      responseContract: 'eval-schedule-create.v1',
    }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: observabilityQueryKeys.evalSchedules() });
    },
  });
}

export function useEvalScheduleAction() {
  const transport = useControlTransport();
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ scheduleId, action }: { scheduleId: string; action: 'pause' | 'resume' | 'cancel' | 'retry' }) => transport.request({ pathId: 'observability.evalSchedule.action', params: { scheduleId }, body: { action } }),
    onSuccess: () => client.invalidateQueries({ queryKey: observabilityQueryKeys.evalSchedules() }),
  });
}

export function useEvalScheduleRuns(scheduleId: string) {
  const transport = useControlTransport();
  return useQuery({
    queryKey: observabilityQueryKeys.evalScheduleRuns(scheduleId),
    enabled: Boolean(scheduleId),
    queryFn: ({ signal }) => transport.request<EvalScheduleRunListV1>({
      pathId: 'observability.evalSchedule.runs',
      params: { scheduleId },
      query: { limit: 100 },
      responseContract: 'eval-schedule-run-list.v1',
      signal,
    }),
    retry: false,
    refetchOnWindowFocus: false,
    refetchInterval: (query) => scheduleRunRefreshInterval(query.state.data),
    refetchIntervalInBackground: false,
  });
}

function hasActiveEval(data: ObservabilityEvalListV1 | undefined): boolean {
  return Boolean(data?.items.some((item) => item.status === 'queued' || item.status === 'running'));
}

function hasActiveSandboxRun(data: SandboxRunList | undefined): boolean {
  return Boolean(data?.items.some((item) => item.status === 'queued' || item.status === 'running'));
}

function scheduleRefreshInterval(data: EvalScheduleListV1 | undefined): number | false {
  if (!data?.items.length) return false;
  const active = data.items.filter((item) => (
    item.status === 'running'
    || (item.status === 'scheduled' && item.runCount < item.maxRuns)
  ));
  if (!active.length) return false;
  if (active.some((item) => item.status === 'running')) return SCHEDULE_ACTIVE_POLL_MS;
  return waitingSchedulePollInterval(active.map((item) => item.nextDueAtMs));
}

function scheduleRunRefreshInterval(data: EvalScheduleRunListV1 | undefined): number | false {
  if (!data) return false;
  const active = data.schedule.status === 'running'
    || (data.schedule.status === 'scheduled' && data.schedule.runCount < data.schedule.maxRuns);
  if (!active) return false;
  if (data.schedule.status === 'running' || data.items.some((item) => item.state === 'claimed')) {
    return SCHEDULE_ACTIVE_POLL_MS;
  }
  return waitingSchedulePollInterval([data.schedule.nextDueAtMs]);
}

function waitingSchedulePollInterval(nextDueAtMs: number[]): number {
  const nextDue = Math.min(...nextDueAtMs.filter((value) => Number.isFinite(value)));
  if (!Number.isFinite(nextDue)) return SCHEDULE_WAITING_POLL_MS;
  const untilDue = nextDue - Date.now();
  if (untilDue <= 0) return SCHEDULE_ACTIVE_POLL_MS;
  return Math.min(SCHEDULE_WAITING_POLL_MS, Math.max(1_000, untilDue));
}

export function mergeObservationEvents(
  left: readonly ObservationEventV1[],
  right: readonly ObservationEventV1[],
  limit: number,
): ObservationEventV1[] {
  const byId = new Map<string, ObservationEventV1>();
  for (const item of [...left, ...right]) byId.set(item.eventId, item);
  return [...byId.values()]
    .sort((a, b) => b.sequence - a.sequence)
    .slice(0, Math.max(1, limit));
}

function queryFilters(filters: ObservationFilters): Record<string, ControlQueryValue> {
  return Object.fromEntries(
    Object.entries(filters).filter((entry): entry is [string, string] => Boolean(entry[1])),
  );
}
