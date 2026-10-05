import type { RoomConversationSnapshot } from '@/contracts/room-reducer';
import { parseRoomConversationSnapshot } from '@/contracts/room-reducer';
import { agentSessionAddress, useAgentLiveStore } from '@/features/agent/state/live-store';
import { useRoomLiveStore } from '@/features/rooms/state/live-store';
import type { ControlTransport } from '@/platform/transport';

/**
 * Conversation warmup is deliberately a projection read. It never attaches
 * an SSE owner and never asks Runtime to ensure/open a Pi Session.
 */
export type ConversationPreloadTarget = {
  kind: 'session' | 'room';
  id: string;
  updatedAtMs?: number;
};

export type ConversationPreloadResult = ConversationPreloadTarget & {
  status: 'ready' | 'cached' | 'failed' | 'cancelled';
  error?: unknown;
};

export interface ConversationPreloadHandle {
  promise: Promise<readonly ConversationPreloadResult[]>;
  cancel(): void;
}

const PRELOAD_CACHE_TTL_MS = 20_000;
const PRELOAD_CACHE_MAX_ENTRIES = 24;
const PRELOAD_DEFAULT_LIMIT = 3;
const PRELOAD_DEFAULT_CONCURRENCY = 2;
const PRELOAD_REQUEST_TIMEOUT_MS = 15_000;

type CacheEntry<T> = {
  loadedAtMs: number;
  value: T;
};

type PendingEntry = {
  controller: AbortController;
  consumers: number;
  promise: Promise<unknown>;
};

type TransportPreloadCache = {
  pending: Map<string, PendingEntry>;
  rooms: Map<string, CacheEntry<RoomConversationSnapshot>>;
  sessions: Map<string, CacheEntry<unknown>>;
};

const caches = new WeakMap<ControlTransport, TransportPreloadCache>();

/** Read or join the bounded recent Session projection request. */
export function readRecentSessionSnapshot(
  transport: ControlTransport,
  sessionId: string,
  signal: AbortSignal,
  options: { useCache?: boolean } = {},
): Promise<unknown> {
  const cache = cacheFor(transport);
  const useCache = options.useCache !== false;
  const cached = useCache ? readCacheEntry(cache.sessions, sessionId) : undefined;
  if (cached) return resolvedUnlessAborted(cached.value, signal);
  if (!useCache) {
    return transport.request({
      pathId: 'agent.session.snapshot',
      params: { sessionId },
      query: { view: 'recent' as const },
      signal,
      timeoutMs: PRELOAD_REQUEST_TIMEOUT_MS,
    }).then((value) => {
      if (signal.aborted) throw abortError();
      saveCacheEntry(cache.sessions, sessionId, value);
      return value;
    });
  }
  return readCached(
    cache,
    `session:${sessionId}`,
    signal,
    (requestSignal) => transport.request({
      pathId: 'agent.session.snapshot',
      params: { sessionId },
      query: { view: 'recent' as const },
      signal: requestSignal,
      timeoutMs: PRELOAD_REQUEST_TIMEOUT_MS,
    }).then((value) => {
      if (requestSignal.aborted) throw abortError();
      saveCacheEntry(cache.sessions, sessionId, value);
      return value;
    }),
  );
}

/** Read or join the lightweight Room conversation projection request. */
export function readRoomConversationSnapshot(
  transport: ControlTransport,
  roomId: string,
  signal: AbortSignal,
  options: { useCache?: boolean } = {},
): Promise<RoomConversationSnapshot> {
  const cache = cacheFor(transport);
  const useCache = options.useCache !== false;
  const cached = useCache ? readCacheEntry(cache.rooms, roomId) : undefined;
  if (cached) return resolvedUnlessAborted(cached.value, signal);
  if (!useCache) {
    return transport.request({
      pathId: 'agent.room.conversationSnapshot',
      params: { roomId },
      signal,
      timeoutMs: PRELOAD_REQUEST_TIMEOUT_MS,
    }).then((value) => {
      if (signal.aborted) throw abortError();
      const snapshot = parseRoomConversationSnapshot(value);
      saveCacheEntry(cache.rooms, roomId, snapshot);
      return snapshot;
    });
  }
  return readCached(
    cache,
    `room:${roomId}`,
    signal,
    async (requestSignal) => {
      const value = parseRoomConversationSnapshot(await transport.request({
        pathId: 'agent.room.conversationSnapshot',
        params: { roomId },
        signal: requestSignal,
        timeoutMs: PRELOAD_REQUEST_TIMEOUT_MS,
      }));
      if (requestSignal.aborted) throw abortError();
      saveCacheEntry(cache.rooms, roomId, value);
      return value;
    },
  );
}

/**
 * Warm the most visible conversations with a small worker pool. The returned
 * handle is cancellable so a hidden/superseded Home does not keep reads alive.
 * Failures are per-target and do not leave a global loading state pending.
 */
export function preloadRecentConversations(
  transport: ControlTransport,
  targets: readonly ConversationPreloadTarget[],
  options: {
    concurrency?: number;
    limit?: number;
    signal?: AbortSignal;
  } = {},
): ConversationPreloadHandle {
  const controller = new AbortController();
  const forwardAbort = () => controller.abort();
  if (options.signal) {
    if (options.signal.aborted) controller.abort();
    else options.signal.addEventListener('abort', forwardAbort, { once: true });
  }

  const limit = Math.max(0, Math.min(options.limit ?? PRELOAD_DEFAULT_LIMIT, PRELOAD_DEFAULT_LIMIT));
  const concurrency = Math.max(1, Math.min(options.concurrency ?? PRELOAD_DEFAULT_CONCURRENCY, PRELOAD_DEFAULT_CONCURRENCY));
  const queue = uniqueTargets(targets).slice(0, limit);

  const promise = (async (): Promise<readonly ConversationPreloadResult[]> => {
    const results: Array<ConversationPreloadResult | undefined> = [];
    let nextIndex = 0;
    const worker = async (): Promise<void> => {
      while (true) {
        if (controller.signal.aborted) return;
        const index = nextIndex++;
        const target = queue[index];
        if (!target) return;
        try {
          const status = await preloadOne(transport, target, controller.signal);
          results[index] = { ...target, status };
        } catch (error) {
          results[index] = {
            ...target,
            status: controller.signal.aborted ? 'cancelled' : 'failed',
            ...(controller.signal.aborted ? {} : { error }),
          };
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(concurrency, queue.length) }, () => worker()));
    if (options.signal) options.signal.removeEventListener('abort', forwardAbort);
    return results.filter((result): result is ConversationPreloadResult => Boolean(result));
  })();

  // A caller may intentionally fire-and-forget the warmup. Keep a cancelled
  // request from becoming an unhandled rejection if the transport aborts
  // before the worker can convert it to a per-target result.
  void promise.catch(() => undefined);
  return {
    promise,
    cancel: () => controller.abort(),
  };
}

async function preloadOne(
  transport: ControlTransport,
  target: ConversationPreloadTarget,
  signal: AbortSignal,
): Promise<'ready' | 'cached'> {
  if (target.kind === 'session') {
    const cached = readCacheEntry(cacheFor(transport).sessions, target.id);
    const value = await readRecentSessionSnapshot(transport, target.id, signal);
    if (signal.aborted) throw abortError();
    useAgentLiveStore.getState().hydrate(agentSessionAddress(transport, target.id), value);
    return cached ? 'cached' : 'ready';
  }
  const cached = readCacheEntry(cacheFor(transport).rooms, target.id);
  const snapshot = await readRoomConversationSnapshot(transport, target.id, signal);
  if (signal.aborted) throw abortError();
  useRoomLiveStore.getState().ensure(target.id);
  // replayConversationSnapshot is intentionally a no-op once a richer
  // projection exists. That preserves switchback history and unknown sends.
  useRoomLiveStore.getState().replayConversationSnapshot(target.id, snapshot);
  return cached ? 'cached' : 'ready';
}

function readCached<T>(
  cache: TransportPreloadCache,
  key: string,
  signal: AbortSignal,
  load: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  const existing = cache.pending.get(key);
  const entry = existing ?? createPending(cache, key, load);
  entry.consumers += 1;
  let released = false;
  const release = () => {
    if (released) return;
    released = true;
    entry.consumers -= 1;
    if (entry.consumers <= 0 && cache.pending.get(key) === entry) {
      cache.pending.delete(key);
      entry.controller.abort();
    }
  };
  if (signal.aborted) {
    release();
    return Promise.reject(abortError());
  }
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => {
      release();
      reject(abortError());
    };
    signal.addEventListener('abort', onAbort, { once: true });
    entry.promise.then(
      (value) => {
        signal.removeEventListener('abort', onAbort);
        if (released) return;
        release();
        resolve(value as T);
      },
      (error) => {
        signal.removeEventListener('abort', onAbort);
        if (released) return;
        release();
        reject(error);
      },
    );
  });
}

function createPending<T>(
  cache: TransportPreloadCache,
  key: string,
  load: (signal: AbortSignal) => Promise<T>,
): PendingEntry {
  const controller = new AbortController();
  let promise: Promise<T>;
  try {
    // Start the transport read in the caller's turn. Besides keeping the
    // foreground open responsive, this preserves the existing request timing
    // contract for synchronous test/native transports.
    promise = load(controller.signal);
  } catch (error) {
    promise = Promise.reject(error);
  }
  const entry: PendingEntry = {
    controller,
    consumers: 0,
    promise,
  };
  cache.pending.set(key, entry);
  entry.promise = entry.promise.finally(() => {
    if (cache.pending.get(key) === entry) cache.pending.delete(key);
  });
  void entry.promise.catch(() => undefined);
  return entry;
}

function cacheFor(transport: ControlTransport): TransportPreloadCache {
  const existing = caches.get(transport);
  if (existing) return existing;
  const created: TransportPreloadCache = {
    pending: new Map(),
    rooms: new Map(),
    sessions: new Map(),
  };
  caches.set(transport, created);
  return created;
}

function readCacheEntry<T>(
  map: Map<string, CacheEntry<T>>,
  key: string,
): CacheEntry<T> | undefined {
  const entry = map.get(key);
  if (!entry || Date.now() - entry.loadedAtMs > PRELOAD_CACHE_TTL_MS) {
    if (entry) map.delete(key);
    return undefined;
  }
  return entry;
}

function saveCacheEntry<T>(
  map: Map<string, CacheEntry<T>>,
  key: string,
  value: T,
): void {
  const loadedAtMs = Date.now();
  for (const [cachedKey, entry] of map) {
    if (loadedAtMs - entry.loadedAtMs > PRELOAD_CACHE_TTL_MS) map.delete(cachedKey);
  }
  map.set(key, { value, loadedAtMs });
  while (map.size > PRELOAD_CACHE_MAX_ENTRIES) {
    let oldestKey: string | undefined;
    let oldestLoadedAt = Number.POSITIVE_INFINITY;
    for (const [cachedKey, entry] of map) {
      if (entry.loadedAtMs < oldestLoadedAt) {
        oldestKey = cachedKey;
        oldestLoadedAt = entry.loadedAtMs;
      }
    }
    if (oldestKey === undefined) return;
    map.delete(oldestKey);
  }
}

function uniqueTargets(targets: readonly ConversationPreloadTarget[]): ConversationPreloadTarget[] {
  const seen = new Set<string>();
  return targets.filter((target) => {
    const id = target.id.trim();
    if (!id) return false;
    const key = `${target.kind}:${id}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).map((target) => ({ ...target, id: target.id.trim() }));
}

function resolvedUnlessAborted<T>(value: T, signal: AbortSignal): Promise<T> {
  if (signal.aborted) return Promise.reject(abortError());
  return Promise.resolve(value);
}

function abortError(): DOMException {
  return new DOMException('Conversation preload was cancelled', 'AbortError');
}
