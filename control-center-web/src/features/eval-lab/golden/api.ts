import { skipToken, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useControlTransport } from '@/app/control-transport';
import type { ControlTransport } from '@/platform/transport';
import { labConnectionKey as goldenConnectionKey, requestLabControl as request } from '../control-request';
import { isActiveJob, isGoldenJob, isGoldenSuite, object, parseGoldenRead, type GoldenAction, type GoldenCommand, type GoldenRead, type GoldenReceipt, type GoldenSuite } from './types';

type PendingCommand = { command: GoldenCommand; outcome: 'sending' | 'unknown' };
const actions: GoldenAction[] = ['create', 'draft', 'review', 'review_case', 'label_sample', 'judge_config', 'calibrate', 'freeze', 'experiment', 'cancel', 'resume'];
export async function getGoldenSuites(transport: ControlTransport, suiteId = '', signal?: AbortSignal): Promise<GoldenRead> {
  const response = await request(transport, {
    pathId: 'agent.eval-lab.golden.get', ...(suiteId ? { query: { suiteId } } : {}), ...(signal ? { signal } : {}),
  });
  return parseGoldenRead(response, suiteId);
}

export async function submitGoldenCommand(transport: ControlTransport, command: GoldenCommand): Promise<GoldenReceipt> {
  const raw = await request(transport, { pathId: 'agent.eval-lab.golden.command', body: command });
  const response = object(raw);
  if (response.ok === false) throw Object.assign(new Error(typeof response.message === 'string' ? response.message : '操作未完成。'), { payload: response });
  if (response.ok !== true || response.clientRequestId !== command.clientRequestId || typeof response.replayed !== 'boolean'
    || !isGoldenSuite(response.suite) || !(response.job === null || isGoldenJob(response.job))
    || (command.suiteId && response.suite.suiteId !== command.suiteId)) throw new Error('收到的回执与本次操作不匹配，请核对本次操作。');
  return response as GoldenReceipt;
}

export function isGoldenRejection(error: unknown): boolean {
  const value = object(error); const details = object(value.details);
  const status = value.status ?? details.status ?? details.statusCode;
  const payload = object(value.payload ?? details.payload ?? details.body ?? value.details);
  if (typeof status === 'number') return status === 409 || status === 422;
  return typeof payload.code === 'string' && /(?:CONFLICT|VALIDATION|INVALID_INPUT|NOT_FOUND)/u.test(payload.code);
}

export function goldenErrorMessage(error: unknown, fallback = '评测服务暂不可用，请重新读取。'): string {
  const value = object(error); const details = object(value.details);
  const payload = object(value.payload ?? details.payload ?? details.body ?? value.details);
  return typeof payload.message === 'string' && payload.message.trim() ? payload.message : fallback;
}

export function goldenPollInterval(data: GoldenRead | undefined, failed: boolean): number | false {
  return !failed && data?.suite?.jobs.some(isActiveJob) ? 2_000 : false;
}

function readSuites(read: GoldenRead | undefined): GoldenSuite[] {
  return read?.suite ? [...read.items, read.suite] : read?.items ?? [];
}

const lastUpdate = (suite: GoldenSuite) => suite.jobs.reduce((latest, job) => Math.max(latest, job.updatedAtMs), suite.updatedAtMs);

function mergeSuites(previous: GoldenSuite[], observed: GoldenSuite[]): GoldenSuite[] {
  const suites = new Map<string, GoldenSuite>();
  for (const suite of [...previous, ...observed]) {
    const known = suites.get(suite.suiteId);
    // Job progress advances independently of the reviewed standard revision.
    if (!known || suite.revision > known.revision || suite.revision === known.revision && lastUpdate(suite) >= lastUpdate(known)) suites.set(suite.suiteId, suite);
  }
  return [...suites.values()].sort((left, right) => right.updatedAtMs - left.updatedAtMs);
}

export function useGoldenWorkflow(suiteId = '') {
  const transport = useControlTransport();
  const client = useQueryClient();
  const connection = goldenConnectionKey(transport);
  const queryPrefix = ['agent-lab-golden', connection] as const;
  const queryKey = [...queryPrefix, suiteId] as const;
  const commandKey = ['agent-lab-golden-command', connection] as const;
  const query = useQuery({
    queryKey,
    queryFn: async ({ signal }) => {
      const observed = await getGoldenSuites(transport, suiteId, signal);
      const items = mergeSuites(readSuites(client.getQueryData<GoldenRead>(queryKey)), readSuites(observed));
      return { ...observed, items, suite: observed.suite ? items.find((suite) => suite.suiteId === observed.suite!.suiteId)! : null };
    },
    initialData: () => {
      if (!suiteId) return undefined;
      // Keep a real catalog receipt if the first suite-specific read fails.
      // Placeholder data disappears on error and used to erase the notebook.
      // Connection and suite identity stay exact; this seed is never fresh.
      const items = mergeSuites([], client.getQueriesData<GoldenRead>({ queryKey: queryPrefix }).flatMap(([, cached]) => readSuites(cached)));
      const selected = items.find((item) => item.suiteId === suiteId);
      return selected ? { ok: true as const, items, suite: selected } : undefined;
    },
    initialDataUpdatedAt: 0,
    retry: false, refetchOnWindowFocus: false, staleTime: 0,
    refetchInterval: (query) => goldenPollInterval(query.state.data, query.state.status === 'error'),
    refetchIntervalInBackground: false,
  });
  const pending = useQuery<PendingCommand | null>({
    queryKey: commandKey, queryFn: skipToken, initialData: () => restore(transport), gcTime: Infinity,
  });
  const mutation = useMutation({
    retry: false,
    mutationFn: async (command: GoldenCommand) => {
      client.setQueryData<PendingCommand>(commandKey, { command, outcome: 'sending' });
      persist(transport, command);
      try {
        const receipt = await submitGoldenCommand(transport, command);
        // A matched receipt remains readable even if the following GET fails.
        // Revision and timestamp prevent an old replay from replacing newer review.
        const reads = client.getQueriesData<GoldenRead>({ queryKey: queryPrefix });
        const items = mergeSuites(reads.flatMap(([, cached]) => readSuites(cached)), [receipt.suite]);
        const accepted = items.find((suite) => suite.suiteId === receipt.suite.suiteId)!;
        for (const [key, cached] of reads) {
          if (cached) client.setQueryData<GoldenRead>(key, { ...cached, items: mergeSuites(cached.items, [accepted]), suite: cached.suite?.suiteId === accepted.suiteId ? accepted : cached.suite });
        }
        client.setQueryData<GoldenRead>([...queryPrefix, accepted.suiteId], { ok: true, items, suite: accepted });
        client.setQueryData(commandKey, null);
        persist(transport, null);
        return receipt;
      } catch (error) {
        const rejected = isGoldenRejection(error);
        client.setQueryData(commandKey, rejected ? null : { command, outcome: 'unknown' });
        if (rejected) persist(transport, null);
        throw error;
      }
    },
    onSettled: async (_receipt, error) => {
      // Refresh current progress after admission; cached receipts are not a live heartbeat.
      if (!error || isGoldenRejection(error)) await client.invalidateQueries({ queryKey: queryPrefix });
    },
  });
  return {
    query, mutation, pending: pending.data ?? null,
    async submit(action: GoldenAction, input: GoldenCommand['input'], suite?: GoldenSuite | null) {
      if (client.getQueryData<PendingCommand | null>(commandKey)) return undefined;
      const command: GoldenCommand = {
        action, ...(suite ? { suiteId: suite.suiteId } : {}), expectedRevision: suite?.revision ?? 0,
        clientRequestId: `lab-golden:${crypto.randomUUID()}`, input: structuredClone(input),
      };
      return mutation.mutateAsync(command).catch(() => undefined);
    },
    async retryPending() {
      const current = client.getQueryData<PendingCommand | null>(commandKey);
      if (!current || current.outcome === 'sending') return undefined;
      return mutation.mutateAsync(current.command).catch(() => undefined);
    },
  };
}

function storageKey(transport: ControlTransport): string | null {
  return transport.connectionIdentity ? `paw.lab.golden-command.v1:${goldenConnectionKey(transport)}` : null;
}
function persist(transport: ControlTransport, command: GoldenCommand | null): void {
  const key = storageKey(transport);
  if (!key) return;
  try {
    if (command) window.sessionStorage.setItem(key, JSON.stringify(command));
    else window.sessionStorage.removeItem(key);
  } catch { /* Query cache keeps the exact command when storage is unavailable. */ }
}
function restore(transport: ControlTransport): PendingCommand | null {
  const key = storageKey(transport);
  if (!key) return null;
  try {
    const value = object(JSON.parse(window.sessionStorage.getItem(key) ?? 'null'));
    if (!actions.includes(value.action as GoldenAction) || !Number.isSafeInteger(value.expectedRevision) || Number(value.expectedRevision) < 0
      || typeof value.clientRequestId !== 'string' || !value.clientRequestId.startsWith('lab-golden:')
      || value.clientRequestId.length > 240 || (value.suiteId !== undefined && typeof value.suiteId !== 'string')
      || value.input === null || typeof value.input !== 'object' || Array.isArray(value.input)) return null;
    return { command: value as GoldenCommand, outcome: 'unknown' };
  } catch { return null; }
}
