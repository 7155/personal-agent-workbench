import { useCallback, useEffect, useRef } from 'react';

import { createRoomDeltaBatcher } from '@/contracts/batching';
import {
  parseRoomConversationSnapshot,
  parseRoomEventSnapshot,
  type RoomConversationSnapshot,
  type RoomEventSnapshot,
} from '@/contracts/room-reducer';
import type { UiRoomEvent } from '@/contracts/ui-events';
import {
  ownerRecoveryDelayMs,
  retryAfterMsFromError,
} from '@/platform/recovery-policy';
import type { ControlTransport } from '@/platform/transport';
import { useRoomLiveStore } from '../state/live-store';

const ROOM_RECOVERY_BASE_DELAY_MS = 1_000;
const ROOM_RECOVERY_MAX_DELAY_MS = 15_000;
const ROOM_RECOVERY_VISIBLE_RETRY_ATTEMPT = 3;
const ROOM_DEFERRED_SNAPSHOT_DELAY_MS = 120;

interface RoomLiveSessionCallbacks {
  onLoadingChange(loading: boolean): void;
  onSnapshot(roomId: string, snapshot: RoomLiveSnapshot): void;
  onMetadata(roomId: string, response: unknown): void;
  onConnectionRestored(roomId: string): void;
  onConnectionError(roomId: string, error: unknown, fallback: string): void;
  onRecoveryState(roomId: string, state: RoomRecoveryState): void;
  onEvents(roomId: string, events: readonly UiRoomEvent[]): void;
}

type RoomRecoveryState = 'recovering' | 'failed' | 'synced';
type RoomLiveSnapshot = RoomConversationSnapshot | RoomEventSnapshot;

interface RoomLiveSessionLease {
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

export function useRoomLiveSession({
  roomId,
  transport,
  active: surfaceActive = true,
  ...callbacks
}: {
  roomId: string;
  transport: ControlTransport;
  active?: boolean;
} & RoomLiveSessionCallbacks): () => void {
  const callbacksRef = useRef<RoomLiveSessionCallbacks>(callbacks);
  const leaseRef = useRef<RoomLiveSessionLease | null>(null);
  const listenerRef = useRef<RoomLiveSessionCallbacks | null>(null);
  callbacksRef.current = callbacks;
  if (!listenerRef.current) {
    listenerRef.current = {
      onLoadingChange: (loading) => callbacksRef.current.onLoadingChange(loading),
      onSnapshot: (activeRoomId, snapshot) => callbacksRef.current.onSnapshot(activeRoomId, snapshot),
      onMetadata: (activeRoomId, response) => callbacksRef.current.onMetadata(activeRoomId, response),
      onConnectionRestored: (activeRoomId) => callbacksRef.current.onConnectionRestored(activeRoomId),
      onConnectionError: (activeRoomId, error, fallback) => (
        callbacksRef.current.onConnectionError(activeRoomId, error, fallback)
      ),
      onRecoveryState: (activeRoomId, state) => callbacksRef.current.onRecoveryState(activeRoomId, state),
      onEvents: (activeRoomId, events) => callbacksRef.current.onEvents(activeRoomId, events),
    };
  }

  useEffect(() => {
    if (!roomId || !surfaceActive) {
      leaseRef.current = null;
      callbacksRef.current.onLoadingChange(false);
      return;
    }
    const lease = getSharedRoomLiveSession(transport, roomId).attach(listenerRef.current!);
    leaseRef.current = lease;
    return () => {
      if (leaseRef.current === lease) leaseRef.current = null;
      lease.release();
    };
  }, [roomId, surfaceActive, transport]);

  return useCallback(() => leaseRef.current?.retry(), []);
}

function getSharedRoomLiveSession(
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

  const broadcast = (notify: (listener: RoomLiveSessionCallbacks) => void) => {
    for (const listener of listeners) {
      try {
        notify(listener);
      } catch (error) {
        // Multiple Room/planet windows share one authoritative subscription.
        // A local render callback is not a transport failure and must not
        // prevent the remaining windows from receiving the same terminal or
        // message event (nor force the SSE cursor into reconnect recovery).
        console.error('Room live-session listener failed', error);
      }
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
  const batcher = createRoomDeltaBatcher((events) => {
    if (!active) return;
    const snapshotRequired = useRoomLiveStore.getState().applyEvents(roomId, events);
    if (snapshotRequired) {
      setRecoveryState('recovering');
      fullSnapshotRequired = true;
      scheduleSnapshotReload();
    } else {
      broadcast((listener) => listener.onEvents(roomId, events));
    }
  });

  async function refreshRoomMetadata(): Promise<void> {
    if (!active || metadataRefreshRunning || !metadataRefreshPending) return;
    metadataRefreshPending = false;
    metadataRefreshRunning = true;
    metadataController = new AbortController();
    try {
      const response = await transport.request({
        pathId: 'agent.room.get',
        params: { roomId },
        signal: metadataController.signal,
      });
      if (active) {
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
      metadataRefreshRunning = false;
      metadataController = undefined;
      if (active && metadataRefreshPending) scheduleMetadataRefresh();
    }
  }

  async function requestFullSnapshot(signal: AbortSignal): Promise<RoomEventSnapshot> {
    return parseRoomEventSnapshot(await transport.request({
      pathId: 'agent.room.snapshot',
      params: { roomId },
      signal,
    }));
  }

  async function requestPreferredSnapshot(
    signal: AbortSignal,
    forceFull: boolean,
  ): Promise<RoomLiveSnapshot> {
    if (forceFull) return requestFullSnapshot(signal);
    try {
      return parseRoomConversationSnapshot(await transport.request({
        pathId: 'agent.room.conversationSnapshot',
        params: { roomId },
        signal,
      }));
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
      const snapshotApplied = useRoomLiveStore
        .getState()
        .replaySnapshotWithTail(roomId, snapshot, liveTail);
      if (snapshotApplied) {
        latestSnapshot = snapshot;
        liveTail = [];
        enrichmentAttempt = 0;
        broadcast((listener) => listener.onSnapshot(roomId, snapshot));
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
    setRecoveryState('recovering');
    setLoading(true);
    const requestGeneration = ++generation;
    const current = useRoomLiveStore.getState().projections[roomId];
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
      );
      if (!active || requestGeneration !== generation) return;
      const store = useRoomLiveStore.getState();
      const conversationSnapshot = snapshot.schemaVersion
        === 'rag-ime.agent-room-conversation-snapshot.v1';
      const snapshotApplied = conversationSnapshot
        ? store.replayConversationSnapshot(roomId, snapshot)
        : store.replaySnapshot(roomId, snapshot);
      if (!snapshotApplied && useRoomLiveStore.getState().projections[roomId]?.needsSnapshot) {
        throw new Error('Room snapshot has not reached the recovery cursor');
      }
      const resumeToken = useRoomLiveStore.getState().projections[roomId]?.resumeToken
        || snapshot.resumeToken;
      setLoading(false);
      if (snapshotApplied) {
        latestSnapshot = snapshot;
        broadcast((listener) => listener.onSnapshot(roomId, snapshot));
      }
      const subscriptionGeneration = requestGeneration;
      unsubscribe = transport.subscribe<UiRoomEvent>(
        {
          pathId: 'agent.room.events',
          params: { roomId },
          lastEventId: resumeToken,
        },
        {
          stable: () => {
            if (!active || subscriptionGeneration !== generation) return;
            markConnectionStable();
          },
          next: (event) => {
            if (!active || subscriptionGeneration !== generation) return;
            // Only a cold conversation projection needs the enrichment tail.
            // The full live store already retains events after enrichment.
            if (!useRoomLiveStore.getState().snapshotsByRoomId[roomId]) liveTail.push(event);
            batcher.push(event);
            if (
              ['room_config_changed', 'topic_changed', 'artifact_changed'].includes(
                event.eventType,
              )
              || (
                event.eventType === 'participant_activity'
                && event.payload.activityKind === 'work'
              )
            ) {
              scheduleMetadataRefresh();
            }
            if (event.eventType !== 'snapshot_required') {
              // Schema validation happens before observer delivery. This is a
              // durable progress proof even for older transports without the
              // optional stable callback.
              markConnectionStable();
            }
          },
          error: (error) => {
            if (active && subscriptionGeneration === generation) {
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
          snapshotRequired: () => {
            if (active && subscriptionGeneration === generation) {
              fullSnapshotRequired = true;
              scheduleSnapshotReload();
            }
          },
        },
      );
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
    attach(listener) {
      const alreadyRunning = active;
      listeners.add(listener);
      if (!alreadyRunning) {
        active = true;
        useRoomLiveStore.getState().ensure(roomId);
        void loadSnapshotAndSubscribe();
      } else {
        listener.onLoadingChange(loading);
        listener.onRecoveryState(roomId, recoveryState);
        if (latestSnapshot) listener.onSnapshot(roomId, latestSnapshot);
        if (hasLatestMetadata) listener.onMetadata(roomId, latestMetadata);
        if (lastError !== undefined) {
          listener.onConnectionError(roomId, lastError, lastErrorFallback);
        } else if (connected) {
          listener.onConnectionRestored(roomId);
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
