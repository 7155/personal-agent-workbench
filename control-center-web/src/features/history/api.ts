import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useControlTransport } from '@/app/control-transport';
import { usePawOsAppActive } from '@/features/paw-os/surface-context';
import type { MutationAvailability } from '@/features/overview/management-mutation';
import { asRecord, stringValue } from '@/features/overview/management-ui';
import type { ControlTransport, JsonValue } from '@/platform/transport';

export const historyQueryKeys = {
  root: ['history'] as const,
  page: (query: string, filter: string) => [...historyQueryKeys.root, 'page', query, filter] as const,
  detail: (eventId: number | null) => [...historyQueryKeys.root, 'detail', eventId] as const,
  capabilities: () => [...historyQueryKeys.root, 'capabilities'] as const,
};

export const historyMutationPathIds = {
  preview: 'history.tombstone.preview',
  apply: 'history.tombstone.apply',
  rollback: 'history.tombstone.rollback',
} as const;

export type HistoryMutationPathId = (typeof historyMutationPathIds)[keyof typeof historyMutationPathIds];

export type HistoryMutationRequest = {
  pathId: HistoryMutationPathId;
  body: Record<string, JsonValue>;
};

export function useHistoryPages(query: string, filter: string) {
  const transport = useControlTransport();
  const active = usePawOsAppActive() ?? true;
  const pages = useInfiniteQuery({
    enabled: active,
    queryKey: historyQueryKeys.page(query, filter),
    queryFn: ({ pageParam, signal }) => transport.request({
      pathId: 'history.page',
      query: { limit: 50, cursor: String(pageParam), ...(query ? { query } : {}), ...(filter ? { filter } : {}) },
      signal,
    }),
    initialPageParam: '',
    getNextPageParam: (lastPage) => stringValue(asRecord(lastPage).nextCursor) || undefined,
  });
  return { pages, transportKind: transport.kind };
}

export function useHistoryDetail(eventId: number | null) {
  const transport = useControlTransport();
  const active = usePawOsAppActive() ?? true;
  return useQuery({
    queryKey: historyQueryKeys.detail(eventId),
    queryFn: () => transport.request({
      pathId: 'history.detail',
      query: { eventId: eventId as number },
    }),
    enabled: active && eventId !== null && Number.isInteger(eventId) && eventId > 0,
  });
}

export function useHistoryMutationBoundary() {
  const transport = useControlTransport();
  const active = usePawOsAppActive() ?? true;
  const capabilities = useQuery({
    enabled: active,
    queryKey: historyQueryKeys.capabilities(),
    queryFn: () => transport.capabilities(),
    staleTime: 30_000,
  });

  const availability = (blockedReason = ''): MutationAvailability => {
    if (capabilities.isPending) return { state: 'checking' };
    if (capabilities.error) return { state: 'unsupported', reason: '暂时无法确认这项操作是否可用，请刷新后重试。' };
    const flags = capabilities.data?.features ?? {};
    const routeIds = new Set((capabilities.data?.routeIds ?? []) as readonly string[]);
    const required = Object.values(historyMutationPathIds);
    if (!flags.managementWorkContract || !flags.historyWorkContract || required.some((pathId) => !routeIds.has(pathId))) {
      return { state: 'unsupported', reason: '当前服务未提供安全的历史记录修改能力；不会执行任何更改。' };
    }
    if (blockedReason) return { state: 'blocked', reason: blockedReason };
    return { state: 'available' };
  };

  return {
    availability,
    capabilities,
    request: <Response,>(request: HistoryMutationRequest) => requestHistoryMutation<Response>(transport, request),
  };
}

export function requestHistoryMutation<Response>(
  transport: ControlTransport,
  request: HistoryMutationRequest,
): Promise<Response> {
  return transport.request<Response>({ pathId: request.pathId, body: request.body });
}
