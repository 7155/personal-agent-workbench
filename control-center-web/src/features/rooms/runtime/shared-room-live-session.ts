import { createRoomDeltaBatcher } from '@/contracts/batching';
import {
  parseRoomEventSnapshot,
  parseRoomEventPage,
  type RoomConversationSnapshot,
  type RoomEventSnapshot,
} from '@/contracts/room-reducer';
import type { UiRoomEvent } from '@/contracts/ui-events';
import {
  ownerRecoveryDelayMs,
  retryAfterMsFromError,
} from '@/platform/recovery-policy';
import type { ControlTransport } from '@/platform/transport';
import { readRoomConversationSnapshot } from '@/features/conversation-ui/conversation-preload';
import { useRoomLiveStore } from '../state/live-store';
import { acceptedRoomEvents, isRoomCursorReset } from '../state/room-event-window';

const ROOM_RECOVERY_BASE_DELAY_MS = 1_000;
const ROOM_RECOVERY_MAX_DELAY_MS = 15_000;
const ROOM_RECOVERY_VISIBLE_RETRY_ATTEMPT = 3;
const ROOM_DEFERRED_SNAPSHOT_DELAY_MS = 120;
// Only the cold-open enrichment needs a second tail. Bound it independently
// of the retained history; overflow schedules a full read, never drops evidence.
const ROOM_ENRICHMENT_TAIL_LIMIT = 2_000;

export interface RoomLiveSessionCallbacks {
  onLoadingChange(loading: boolean): void;
  onSnapshot(roomId: string, snapshot: RoomLiveSnapshot): void;
  onMetadata(roomId: string, response: unknown): void;
  onConnectionRestored(roomId: string): void;
  onConnectionError(roomId: string, error: unknown, fallback: string): void;
  onRecoveryState(roomId: string, state: RoomRecoveryState): void;
  onEvents(roomId: string, events: readonly UiRoomEvent[]): void;
}

export type RoomRecoveryState = 'recovering' | 'failed' | 'synced';
export type RoomLiveSnapshot = RoomConversationSnapshot | RoomEventSnapshot;

export interface RoomLiveSessionLease {
  retry(): void;
  release(): void;
}

interface SharedRoomLiveSession {
  attach(listener: RoomLiveSessionCallbacks): RoomLiveSessionLease;
}

const sharedRoomLiveSessions = new WeakMap<
ControlTransport,
Map<string, SharedRoomLiveSession>
>();

export function getSharedRoomLiveSession(
  transport: ControlTransport,
  roomId: string,
): SharedRoomLiveSession {
  let transportSessions = sharedRoomLiveSessions.get(transport);
  if (!transportSessions) {
    transportSessions = new Map();
    sharedRoomLiveSessions.set(transport, transportSessions);
  }
  const existing = transportSessions.get(roomId);
  if (existing) return existing;
  let session: SharedRoomLiveSession;
  session = createSharedRoomLiveSession(transport, roomId, () => {
    if (transportSessions?.get(roomId) === session) transportSessions.delete(roomId);
  });
  transportSessions.set(roomId, session);
  return session;
}

function createSharedRoomLiveSession(
  transport: ControlTransport,
  roomId: string,
  onEmpty: () => void,
): SharedRoomLiveSession {
  const listeners = new Set<RoomLiveSessionCallbacks>();
  let active = false;
  let generation = 0;
  let reloadQueued = false;
  let snapshotRunning = false;
  let snapshotReloadPending = false;
  let recoveryAttempt = 0;
  let recoveryTimer: ReturnType<typeof setTimeout> | undefined;
  let metadataRefreshQueued = false;
  let metadataRefreshRunning = false;
  let metadataRefreshPending = false;
  let unsubscribe: (() => void) | undefined;
  let snapshotController: AbortController | undefined;
  let metadataController: AbortController | undefined;
  let enrichmentController: AbortController | undefined;
  let enrichmentTimer: ReturnType<typeof setTimeout> | undefined;
  let enrichmentAttempt = 0;
  let fullSnapshotRequired = false;
  let liveTail: UiRoomEvent[] = [];
  let loading = false;
  let recoveryState: RoomRecoveryState = 'recovering';
  let connected = false;
  let latestSnapshot: RoomLiveSnapshot | undefined;
  let latestMetadata: unknown;
  let hasLatestMetadata = false;
  let lastError: unknown;
  let lastErrorFallback = '';

  const notifyListener = (listener: RoomLiveSessionCallbacks, notify: (listener: RoomLiveSessionCallbacks) => void) => {
      try {
        notify(listener);
      } catch (error) {
        // Multiple Room/planet windows share one authoritative subscription.
        // A local render callback is not a transport failure and must not
        // prevent the remaining windows from receiving the same terminal or
        // message event (nor force the SSE cursor into reconnect recovery).
        console.error('Room live-session listener failed', error);
      }
  };
  const broadcast = (notify: (listener: RoomLiveSessionCallbacks) => void) => {
    for (const listener of listeners) notifyListener(listener, notify);
  };
  const publishSnapshot = (snapshot: RoomLiveSnapshot) => {
    latestSnapshot = snapshot;
    broadcast((listener) => listener.onSnapshot(roomId, snapshot));
    // A deferred full-event enrichment can predate a fresher roster read.
    // Enrich tool evidence without leaving consumers on older metadata.
    if (hasLatestMetadata && metadataCursor(latestMetadata) > snapshot.room.lastEventSequence) {
      broadcast((listener) => listener.onMetadata(roomId, latestMetadata));
    }
  };
  const setLoading = (next: boolean) => {
    loading = next;
    broadcast((listener) => listener.onLoadingChange(next));
  };
  const setRecoveryState = (next: RoomRecoveryState) => {
    recoveryState = next;
    broadcast((listener) => listener.onRecoveryState(roomId, next));
  };
  const clearRecoveryTimer = () => {
    if (recoveryTimer === undefined) return;
    clearTimeout(recoveryTimer);
    recoveryTimer = undefined;
  };
  const clearDeferredSnapshot = () => {
    if (enrichmentTimer !== undefined) {
      clearTimeout(enrichmentTimer);
      enrichmentTimer = undefined;
    }
    enrichmentController?.abort();
    enrichmentController = undefined;
  };
  const resetRecoveryBackoff = () => {
    recoveryAttempt = 0;
    clearRecoveryTimer();
  };
  const markConnectionStable = () => {
    if (useRoomLiveStore.getState().projections[roomId]?.needsSnapshot) return;
    resetRecoveryBackoff();
    if (connected && recoveryState === 'synced') return;
    connected = true;
    lastError = undefined;
    lastErrorFallback = '';
    setRecoveryState('synced');
    broadcast((listener) => listener.onConnectionRestored(roomId));
  };
  const scheduleSnapshotReload = (resetBackoff = false) => {
    if (resetBackoff) resetRecoveryBackoff();
    else clearRecoveryTimer();
    if (snapshotRunning) {
      snapshotReloadPending = true;
      return;
    }
    if (!active || reloadQueued) return;
    reloadQueued = true;
    queueMicrotask(() => {
      reloadQueued = false;
      if (active) void loadSnapshotAndSubscribe();
    });
  };
  const retry = () => {
    if (!active) return;
    setRecoveryState('recovering');
    scheduleSnapshotReload(true);
  };
  const scheduleAutomaticRecovery = (error?: unknown) => {
    if (!active || recoveryTimer !== undefined || reloadQueued || snapshotReloadPending) return;
    const attempt = recoveryAttempt + 1;
    const delayMs = ownerRecoveryDelayMs({
      ownerId: `room:${roomId}`,
      attempt: recoveryAttempt,
      baseDelayMs: ROOM_RECOVERY_BASE_DELAY_MS,
      maxDelayMs: ROOM_RECOVERY_MAX_DELAY_MS,
      retryAfterMs: retryAfterMsFromError(error),
    });
    recoveryAttempt = attempt;
    setRecoveryState(attempt >= ROOM_RECOVERY_VISIBLE_RETRY_ATTEMPT ? 'failed' : 'recovering');
    recoveryTimer = setTimeout(() => {
      recoveryTimer = undefined;
      if (active) scheduleSnapshotReload();
    }, delayMs);
  };
  const scheduleMetadataRefresh = () => {
    if (!active) return;
    metadataRefreshPending = true;
    if (metadataRefreshQueued || metadataRefreshRunning) return;
    metadataRefreshQueued = true;
    queueMicrotask(() => {
      metadataRefreshQueued = false;
      if (active) void refreshRoomMetadata();
    });
  };
  const requireFullSnapshot = () => {
    if (!active) return;
    fullSnapshotRequired = true;
    connected = false;
    // Invalidate synchronously; callbacks already queued by an old stream must
    // not advertise a healthy connection while the recovery read is pending.
    generation += 1;
    unsubscribe?.();
    unsubscribe = undefined;
    clearDeferredSnapshot();
    liveTail = [];
    setRecoveryState('recovering');
    scheduleSnapshotReload();
  };
  const batcher = createRoomDeltaBatcher((events) => {
    if (!active) return;
    const before = useRoomLiveStore.getState().projections[roomId];
    const snapshotRequired = useRoomLiveStore.getState().applyEvents(roomId, events);
    const after = useRoomLiveStore.getState().projections[roomId];
    const accepted = before && after ? acceptedRoomEvents(before, after, events) : [];
    if (accepted.length) {
      broadcast((listener) => listener.onEvents(roomId, accepted));
      if (accepted.some((event) => ['room_config_changed', 'topic_changed', 'artifact_changed'].includes(event.eventType)
        || (event.eventType === 'participant_activity' && event.payload.activityKind === 'work'))) {
        scheduleMetadataRefresh();
      }
    }
    if (snapshotRequired) requireFullSnapshot();
    else if (accepted.length) markConnectionStable();
  });

  async function refreshRoomMetadata(): Promise<void> {
    if (!active || metadataRefreshRunning || !metadataRefreshPending) return;
    metadataRefreshPending = false;
    metadataRefreshRunning = true;
    const requestGeneration = generation;
    const requestedCursor = useRoomLiveStore.getState().projections[roomId]?.lastSequence ?? 0;
    const controller = new AbortController();
    metadataController = controller;
    try {
      const response = await transport.request({
        pathId: 'agent.room.get',
        params: { roomId },
        signal: controller.signal,
      });
      if (active && requestGeneration === generation && !controller.signal.aborted
        && metadataRoomId(response) === roomId
        && metadataCursor(response) >= Math.max(requestedCursor, metadataCursor(latestMetadata), latestSnapshot?.room.lastEventSequence ?? 0)) {
        latestMetadata = response;
        hasLatestMetadata = true;
        broadcast((listener) => listener.onMetadata(roomId, response));
      }
    } catch (error) {
      // Metadata is best-effort. The snapshot and event stream remain the
      // authoritative Room projection, so a late detail refresh must not
      // turn a healthy live conversation into a global timeout state.
      void error;
    } finally {
      if (metadataController === controller) {
        metadataRefreshRunning = false;
        metadataController = undefined;
        if (active && metadataRefreshPending) scheduleMetadataRefresh();
      }
    }
  }

  async function requestFullSnapshot(signal: AbortSignal): Promise<RoomEventSnapshot> {
    const snapshot = parseRoomEventSnapshot(await transport.request({
      pathId: 'agent.room.snapshot',
      params: { roomId },
      signal,
      timeoutMs: 15000,
    }));
    const current = useRoomLiveStore.getState();
    const cached = current.historyByRoomId[roomId];
    if (cached?.firstSequence === 1 && !(current.projections[roomId] && isRoomCursorReset(current.projections[roomId]))
      && cached.events.some(event => event.sequence === snapshot.firstSequence - 1)) return snapshot;
    // Show the lightweight conversation immediately, then fill the persisted
    // prefix in bounded pages. Never replace it with a tail that silently
    // forgets its Jev owner or the earlier partner/tool receipts.
    const pages: UiRoomEvent[][] = [];
    let before = snapshot.firstSequence;
    while (before > 1) {
      signal.throwIfAborted();
      const page = parseRoomEventPage(await transport.request({
        pathId: 'agent.room.history', params: { roomId },
        query: { beforeSequence: before, limit: 200 }, signal, timeoutMs: 15000,
      }));
      if (page.roomId !== roomId) throw new TypeError('Room history belongs to another Room');
      if (!page.items.length) break;
      if (page.lastSequence !== before - 1 || page.firstSequence >= before) {
        throw new TypeError('Room history did not extend the requested prefix');
      }
      pages.push(page.items);
      before = page.firstSequence;
      if (!page.hasMore) break;
    }
    if (!pages.length) return snapshot;
    const events = [...pages.reverse().flat(), ...snapshot.events];
    return { ...snapshot, events, firstSequence: events[0]!.sequence, truncated: events[0]!.sequence > 1 };
  }

  async function requestPreferredSnapshot(
    signal: AbortSignal,
    forceFull: boolean,
    useConversationCache: boolean,
  ): Promise<RoomLiveSnapshot> {
    if (forceFull) return requestFullSnapshot(signal);
    try {
      return await readRoomConversationSnapshot(transport, roomId, signal, {
        useCache: useConversationCache,
      });
    } catch (error) {
      // Rolling upgrades can briefly pair a new frontend with an older local
      // Runtime. The existing full snapshot is the safe compatibility path.
      if (isAbortError(error)) throw error;
      return requestFullSnapshot(signal);
    }
  }

  const scheduleDeferredSnapshot = (requestGeneration: number) => {
    if (
      !active
      || requestGeneration !== generation
      || useRoomLiveStore.getState().snapshotsByRoomId[roomId]
    ) return;
    if (enrichmentTimer !== undefined || enrichmentController) return;
    enrichmentTimer = setTimeout(() => {
      enrichmentTimer = undefined;
      if (active && requestGeneration === generation) {
        void loadDeferredSnapshot(requestGeneration);
      }
    }, ROOM_DEFERRED_SNAPSHOT_DELAY_MS);
  };

  async function loadDeferredSnapshot(requestGeneration: number): Promise<void> {
    if (!active || requestGeneration !== generation || enrichmentController) return;
    const controller = new AbortController();
    enrichmentController = controller;
    try {
      const snapshot = await requestFullSnapshot(controller.signal);
      if (!active || requestGeneration !== generation) return;
      batcher.flush();
      if (!active || requestGeneration !== generation) return;
      const snapshotApplied = useRoomLiveStore
        .getState()
        .replaySnapshotWithTail(roomId, snapshot, liveTail);
      if (!snapshotApplied) {
        requireFullSnapshot();
        return;
      }
      if (snapshotApplied) {
        liveTail = [];
        enrichmentAttempt = 0;
        publishSnapshot(snapshot);
      }
    } catch (error) {
      if (
        active
        && requestGeneration === generation
        && !isAbortError(error)
        && enrichmentAttempt < 2
      ) {
        enrichmentAttempt += 1;
        enrichmentTimer = setTimeout(() => {
          enrichmentTimer = undefined;
          if (active && requestGeneration === generation) {
            void loadDeferredSnapshot(requestGeneration);
          }
        }, ROOM_RECOVERY_BASE_DELAY_MS * (2 ** enrichmentAttempt));
      } else if (active && requestGeneration === generation && !isAbortError(error)) {
        requireFullSnapshot();
      }
    } finally {
      if (enrichmentController === controller) enrichmentController = undefined;
    }
  }

  async function loadSnapshotAndSubscribe(): Promise<void> {
    if (snapshotRunning) {
      snapshotReloadPending = true;
      return;
    }
    snapshotRunning = true;
    batcher.flush();
    // Any reload requested by the flush is covered by the read starting now.
    snapshotReloadPending = false;
    setRecoveryState('recovering');
    setLoading(true);
    const requestGeneration = ++generation;
    metadataController?.abort();
    metadataController = undefined;
    metadataRefreshRunning = false;
    metadataRefreshPending = false;
    const current = useRoomLiveStore.getState().projections[roomId];
    if (current && isRoomCursorReset(current)) {
      latestSnapshot = undefined;
      latestMetadata = undefined;
      hasLatestMetadata = false;
    }
    const forceFull = fullSnapshotRequired || Boolean(current?.needsSnapshot);
    fullSnapshotRequired = false;
    enrichmentAttempt = 0;
    clearDeferredSnapshot();
    liveTail = [];
    batcher.clear();
    unsubscribe?.();
    unsubscribe = undefined;
    connected = false;
    snapshotController = new AbortController();
    try {
      const snapshot = await requestPreferredSnapshot(
        snapshotController.signal,
        forceFull,
        latestSnapshot === undefined,
      );
      if (!active || requestGeneration !== generation) return;
      if (snapshot.room.id !== roomId) throw new TypeError('Room snapshot belongs to another Room');
      const store = useRoomLiveStore.getState();
      const conversationSnapshot = snapshot.schemaVersion
        === 'rag-ime.agent-room-conversation-snapshot.v1';
      const projectionBefore = store.projections[roomId];
      const snapshotApplied = conversationSnapshot
        ? store.replayConversationSnapshot(roomId, snapshot)
        : store.replaySnapshot(roomId, snapshot);
      const cachedConversationReady = conversationSnapshot
        && !projectionBefore?.needsSnapshot
        && 'cursorSequence' in snapshot
        && projectionBefore?.lastSequence === snapshot.cursorSequence;
      if (!snapshotApplied && useRoomLiveStore.getState().projections[roomId]?.needsSnapshot) {
        throw new Error('Room snapshot has not reached the recovery cursor');
      }
      const resumeToken = useRoomLiveStore.getState().projections[roomId]?.resumeToken
        || snapshot.resumeToken;
      setLoading(false);
      if (snapshotApplied || cachedConversationReady) {
        publishSnapshot(snapshot);
      }
      const subscriptionGeneration = requestGeneration;
      const subscription = transport.subscribe<UiRoomEvent>(
        {
          pathId: 'agent.room.events',
          params: { roomId },
          lastEventId: resumeToken,
        },
        {
          stable: () => {
            if (!active || subscriptionGeneration !== generation) return;
            // Per-event stable callbacks must not defeat the 80ms batcher.
            // Pending deltas establish stability only after their reduction.
            if (batcher.pendingCount === 0) markConnectionStable();
          },
          next: (event) => {
            if (!active || subscriptionGeneration !== generation) return;
            if (!useRoomLiveStore.getState().snapshotsByRoomId[roomId]
              && event.eventType !== 'snapshot_required') {
              if (liveTail.length >= ROOM_ENRICHMENT_TAIL_LIMIT) {
                batcher.push(event);
                batcher.flush();
                requireFullSnapshot();
                return;
              }
              liveTail.push(event);
            }
            // Stable only after an accepted contiguous reduction, not merely
            // after a frame has arrived in the batcher's pending queue.
            batcher.push(event);
          },
          error: (error) => {
            if (active && subscriptionGeneration === generation) {
              batcher.flush();
              if (!active || subscriptionGeneration !== generation) return;
              unsubscribe?.();
              unsubscribe = undefined;
              generation += 1;
              clearDeferredSnapshot();
              connected = false;
              lastError = error;
              lastErrorFallback = 'Room 实时连接暂时中断，请稍后重试。';
              setRecoveryState('failed');
              broadcast((listener) => listener.onConnectionError(
                roomId,
                error,
                lastErrorFallback,
              ));
              scheduleAutomaticRecovery(error);
            }
          },
          snapshotRequired: (event) => {
            if (active && subscriptionGeneration === generation) {
              // Also support transports that deliver a control only here.
              if (event?.eventType === 'snapshot_required') batcher.push(event);
              else {
                batcher.flush();
                if (subscriptionGeneration === generation) requireFullSnapshot();
              }
            }
          },
        },
      );
      // subscribe() may synchronously report an error/control before returning
      // its disposer. Never retain that stale subscription in this race.
      if (active && subscriptionGeneration === generation) unsubscribe = subscription;
      else subscription();
      if (conversationSnapshot) scheduleDeferredSnapshot(requestGeneration);
      // Connection state is restored only by a stable frame/event callback,
      // not merely because subscription setup or HTTP headers returned.
    } catch (error) {
      if (
        active
        && requestGeneration === generation
        && !isAbortError(error)
      ) {
        connected = false;
        lastError = error;
        lastErrorFallback = '暂时无法同步 Room 对话；已显示的历史消息会保留，实时更新已暂停。';
        setRecoveryState('failed');
        broadcast((listener) => listener.onConnectionError(
          roomId,
          error,
          lastErrorFallback,
        ));
        setLoading(false);
        scheduleAutomaticRecovery(error);
      }
    } finally {
      snapshotRunning = false;
      snapshotController = undefined;
      if (active && snapshotReloadPending) {
        snapshotReloadPending = false;
        scheduleSnapshotReload();
      }
    }
  }

  const stop = () => {
    active = false;
    generation += 1;
    clearRecoveryTimer();
    clearDeferredSnapshot();
    snapshotController?.abort();
    metadataController?.abort();
    batcher.clear();
    unsubscribe?.();
    unsubscribe = undefined;
  };

  return {
    attach(callbacks) {
      // Each lease has a distinct identity, even when callers reuse callbacks.
      const listener = { ...callbacks };
      const alreadyRunning = active;
      listeners.add(listener);
      if (!alreadyRunning) {
        active = true;
        useRoomLiveStore.getState().ensure(roomId);
        void loadSnapshotAndSubscribe();
      } else {
        notifyListener(listener, (target) => target.onLoadingChange(loading));
        notifyListener(listener, (target) => target.onRecoveryState(roomId, recoveryState));
        if (latestSnapshot) notifyListener(listener, (target) => target.onSnapshot(roomId, latestSnapshot!));
        if (hasLatestMetadata) notifyListener(listener, (target) => target.onMetadata(roomId, latestMetadata));
        if (lastError !== undefined) {
          notifyListener(listener, (target) => target.onConnectionError(roomId, lastError, lastErrorFallback));
        } else if (connected) {
          notifyListener(listener, (target) => target.onConnectionRestored(roomId));
        }
      }
      let released = false;
      return {
        retry,
        release() {
          if (released) return;
          released = true;
          listeners.delete(listener);
          if (listeners.size > 0) return;
          stop();
          onEmpty();
        },
      };
    },
  };
}

function isAbortError(value: unknown): boolean {
  return value instanceof DOMException && value.name === 'AbortError';
}

/** Metadata carries its own snapshot cursor. An unrelated/older response is
 * never a replacement roster. Do not infer it from arrival time. */
function metadataCursor(value: unknown): number {
  if (typeof value !== 'object' || value === null) return -1;
  const room = (value as { room?: { lastEventSequence?: unknown } }).room;
  const cursor = room?.lastEventSequence;
  return typeof cursor === 'number' && Number.isSafeInteger(cursor) && cursor >= 0 ? cursor : -1;
}

function metadataRoomId(value: unknown): unknown {
  if (typeof value !== 'object' || value === null) return undefined;
  return (value as { room?: { id?: unknown } }).room?.id;
}
