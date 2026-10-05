import { QueryClientContext, QueryObserver, queryOptions, type DefaultError, type FetchQueryOptions, type Query, type QueryClient, type QueryKey } from '@tanstack/react-query';
import { useContext } from 'react';
import { queryClient as defaultControlQueryClient } from '@/app/query-client';
import type { ControlTransport } from '@/platform/transport';
import { controlTransportScopeKey } from '@/platform/transport-scope';
import { requireCapabilityCatalog, requireSessionCapabilityCatalog } from './capability-policy';

/** Embedded surfaces also work without an App Center provider. They still
 * share the product QueryClient, with every cache key scoped to transport. */
export function useCatalogQueryClient(): QueryClient {
  return useContext(QueryClientContext) ?? defaultControlQueryClient;
}

/** Borrow one existing Query observer for an imperative catalog consumer.
 * Cancelling this reader releases only its lease; React Query cancels the
 * transport request only after the last observer leaves. */
export function readCatalogQuery<T, TKey extends QueryKey>(client: QueryClient, options: FetchQueryOptions<T, DefaultError, T, TKey>, signal?: AbortSignal): Promise<T> {
  if (signal?.aborted) return Promise.reject(new DOMException('Catalog read cancelled', 'AbortError'));
  const observer = new QueryObserver<T, DefaultError, T, T, TKey>(client, { ...options, enabled: false });
  const unsubscribe = observer.subscribe(() => undefined);
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const finish = (callback: () => void) => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener('abort', cancel);
      unsubscribe();
      callback();
    };
    const cancel = () => finish(() => reject(new DOMException('Catalog read cancelled', 'AbortError')));
    signal?.addEventListener('abort', cancel, { once: true });
    client.fetchQuery(options).then(value => finish(() => resolve(value)), error => finish(() => reject(error)));
  });
}

/** A mounted projection keeps observing the same cache until its existing
 * lifecycle signal is released, including later authoritative refreshes. */
export function observeCatalogQuery<T, TKey extends QueryKey>(
  client: QueryClient,
  options: FetchQueryOptions<T, DefaultError, T, TKey>,
  signal: AbortSignal,
  handlers: { onData(value: T): void; onError(error: unknown): void; onFetching?(): void },
): Promise<T> {
  if (signal.aborted) return Promise.reject(new DOMException('Catalog read cancelled', 'AbortError'));
  const observer = new QueryObserver<T, DefaultError, T, T, TKey>(client, { ...options, enabled: true, refetchOnMount: false });
  return new Promise<T>((resolve, reject) => {
    let initialSettled = false;
    let unsubscribe: () => void = () => undefined;
    let dataCount = -1;
    let errorCount = -1;
    const deliver = () => {
      if (signal.aborted) return;
      const result = observer.getCurrentResult();
      const state = observer.getCurrentQuery().state;
      if (result.fetchStatus === 'fetching') { handlers.onFetching?.(); return; }
      if (result.isSuccess && !state.isInvalidated && state.dataUpdateCount !== dataCount) {
        dataCount = state.dataUpdateCount;
        handlers.onData(result.data);
      } else if (result.isError && state.errorUpdateCount !== errorCount) {
        errorCount = state.errorUpdateCount;
        handlers.onError(result.error);
      }
    };
    const cancel = () => {
      signal.removeEventListener('abort', cancel);
      unsubscribe();
      if (!initialSettled) { initialSettled = true; reject(new DOMException('Catalog read cancelled', 'AbortError')); }
    };
    signal.addEventListener('abort', cancel, { once: true });
    unsubscribe = observer.subscribe(deliver);
    client.fetchQuery(options).then(value => {
      if (signal.aborted) return;
      deliver();
      if (!initialSettled) { initialSettled = true; resolve(value); }
    }, error => {
      if (signal.aborted) return;
      deliver();
      if (!initialSettled) { initialSettled = true; reject(error); }
    });
  });
}

/** Capture before notifying consumers. A pre-receipt read may finish normally,
 * but cannot satisfy the post-receipt refresh. Existing Query promises identify
 * whether another consumer already started the required trailing read. */
export function prepareCatalogRefresh(client: QueryClient, keys: readonly QueryKey[]): () => Promise<void> {
  const previousFlights = new Map<Query, Promise<unknown>>();
  for (const queryKey of keys) for (const query of client.getQueryCache().findAll({ queryKey })) {
    if (query.state.fetchStatus === 'fetching' && query.promise) previousFlights.set(query, query.promise);
  }
  return async () => {
    await Promise.all(keys.map(queryKey => client.invalidateQueries({ queryKey }, { cancelRefetch: false })));
    // Invalidation only awaits active observers. An imperative or disabled
    // reader can still finish later and clear isInvalidated with old data.
    // Fence that captured flight too, then leave inactive data stale without
    // eagerly fetching for an absent consumer.
    await Promise.all([...previousFlights.values()].map(promise => promise.catch(() => undefined)));
    const needsTrailing = new Set<Query>();
    const newerFlights: Promise<unknown>[] = [];
    for (const [query, promise] of previousFlights) {
      if (client.getQueryCache().get(query.queryHash) !== query) continue;
      if (query.promise === promise) needsTrailing.add(query);
      else if (query.state.fetchStatus === 'fetching' && query.promise) newerFlights.push(query.promise);
    }
    await Promise.all([
      ...(needsTrailing.size ? [client.invalidateQueries({ predicate: query => needsTrailing.has(query) }, { cancelRefetch: false })] : []),
      ...newerFlights.map(promise => promise.catch(() => undefined)),
    ]);
  };
}

export const pluginQueryKeys = {
  root: (transport: ControlTransport) => ['plugins', controlTransportScopeKey(transport)] as const,
  catalogs: (transport: ControlTransport) => [...pluginQueryKeys.root(transport), 'catalog'] as const,
  catalog: (transport: ControlTransport, sessionId = '') => [...pluginQueryKeys.catalogs(transport), sessionId] as const,
  skills: (transport: ControlTransport) => [...pluginQueryKeys.root(transport), 'skills'] as const,
  skill: (transport: ControlTransport, skillId: string) => [...pluginQueryKeys.skills(transport), skillId] as const,
  defaults: (transport: ControlTransport) => [...pluginQueryKeys.root(transport), 'defaults'] as const,
  installed: (transport: ControlTransport) => [...pluginQueryKeys.root(transport), 'installed'] as const,
  versions: (transport: ControlTransport) => [...pluginQueryKeys.root(transport), 'versions'] as const,
  proposals: (transport: ControlTransport) => [...pluginQueryKeys.root(transport), 'proposals'] as const,
  lifecycle: (transport: ControlTransport) => [...pluginQueryKeys.root(transport), 'lifecycle'] as const,
};

/** App Center and Session controls read the same validated policy projection.
 * This cache grants no authority; commands remain backend-validated. */
export function capabilityCatalogQueryOptions(transport: ControlTransport, sessionId = '') {
  return queryOptions({
    queryKey: pluginQueryKeys.catalog(transport, sessionId),
    queryFn: async ({ signal }) => {
      const response = await transport.request({ pathId: 'agent.tools.list', ...(sessionId ? { query: { sessionId } } : {}), signal });
      return sessionId ? requireSessionCapabilityCatalog(response, sessionId) : requireCapabilityCatalog(response);
    },
    staleTime: 30_000,
    retry: false,
    refetchOnReconnect: 'always' as const,
  });
}

/** Installed package evidence has distinct consumers, but only one read key. */
export function extensionInventoryQueryOptions(transport: ControlTransport) {
  return queryOptions({
    queryKey: pluginQueryKeys.installed(transport),
    queryFn: ({ signal }) => transport.request({ pathId: 'agent.extensions.list', signal }),
    staleTime: 5_000,
    // The installation owner already has a passive reconciliation cadence.
    // A shared App Center client must not add hidden immediate retries.
    retry: false,
  });
}
