import { useCallback, useEffect, useRef } from 'react';

import { createAgentDeltaBatcher } from '@/contracts/batching';
import type { UiAgentEvent } from '@/contracts/ui-events';
import {
  ownerRecoveryDelayMs,
  retryAfterMsFromError,
} from '@/platform/recovery-policy';
import type { ControlTransport } from '@/platform/transport';
import { agentProjection, useAgentLiveStore } from '../state/live-store';
import { createSnapshotRequestQueue } from './snapshot-request-queue';

export type AgentSnapshotView = 'recent' | 'full';
export type AgentRecoveryState = 'recovering' | 'failed' | 'synced';

export interface AgentLiveSnapshot {
  sessionId: string;
  value: unknown;
  view: AgentSnapshotView;
  presentable: boolean;
  hydrated: boolean;
  sequence: number;
  resumeToken: string;
}

export interface AgentLiveSnapshotError {
  sessionId: string;
  view: AgentSnapshotView;
  error: unknown;
  recoverable: boolean;
}

export interface AgentLiveSessionCallbacks {
  onLoadingChange?(loading: boolean): void;
  onRecoveryState?(state: AgentRecoveryState): void;
  onSnapshot?(snapshot: AgentLiveSnapshot): void;
  onSnapshotError?(failure: AgentLiveSnapshotError): void;
  /** Called only after this event is committed to the shared projection. */
  onEvent?(event: UiAgentEvent): void;
  onEvents?(events: readonly UiAgentEvent[]): void;
  onConnectionRestored?(sessionId: string): void;
  onConnectionError?(sessionId: string, error: unknown): void;
}

export interface AgentLiveSnapshotRequest {
  preserveAfterSequence?: number;
  view?: AgentSnapshotView;
}

export type AgentLiveSnapshotLoader = (
  request?: AgentLiveSnapshotRequest,
) => Promise<boolean>;

export interface AgentLiveSessionOptions extends AgentLiveSessionCallbacks {
  sessionId: string;
  transport: ControlTransport;
  active?: boolean;
  live?: boolean;
  snapshotView?: AgentSnapshotView;
}

interface AgentLiveSessionLease {
  loadSnapshot(request?: AgentLiveSnapshotRequest): Promise<boolean>;
  update(options: { live: boolean; snapshotView: AgentSnapshotView }): void;
  release(): void;
}

interface SharedAgentLiveSession {
  attach(
    listener: AgentLiveSessionCallbacks,
    options: { live: boolean; snapshotView: AgentSnapshotView },
  ): AgentLiveSessionLease;
}

interface ListenerState {
  listener: AgentLiveSessionCallbacks;
  live: boolean;
  snapshotView: AgentSnapshotView;
}

const sharedAgentLiveSessions = new WeakMap<
  ControlTransport,
  Map<string, SharedAgentLiveSession>
>();

const AGENT_RECOVERY_BASE_DELAY_MS = 1_000;
const AGENT_RECOVERY_MAX_DELAY_MS = 8_000;
const AGENT_RECOVERY_VISIBLE_FAILURE_ATTEMPT = 3;

export function useAgentLiveSession({
  sessionId,
  transport,
  active: surfaceActive = true,
  live: liveSurface = true,
  snapshotView = 'recent',
  ...callbacks
}: AgentLiveSessionOptions): AgentLiveSnapshotLoader {
  const callbacksRef = useRef<AgentLiveSessionCallbacks>(callbacks);
  const scopeRef = useRef({ sessionId, transport, surfaceActive });
  const leaseRef = useRef<AgentLiveSessionLease | undefined>(undefined);
  callbacksRef.current = callbacks;
  scopeRef.current = { sessionId, transport, surfaceActive };

  useEffect(() => {
    if (!sessionId || !surfaceActive) {
      leaseRef.current?.release();
      leaseRef.current = undefined;
      callbacksRef.current.onLoadingChange?.(false);
      return;
    }
    const notify = (call: (current: AgentLiveSessionCallbacks) => void) => {
      const scope = scopeRef.current;
      // A render can select another Session before the old effect cleans up.
      // Never forward that old owner's event into the new window callbacks.
      if (scope.sessionId !== sessionId || scope.transport !== transport || !scope.surfaceActive) return;
      call(callbacksRef.current);
    };
    const listener: AgentLiveSessionCallbacks = {
      onLoadingChange: (value) => notify((current) => current.onLoadingChange?.(value)),
      onRecoveryState: (value) => notify((current) => current.onRecoveryState?.(value)),
      onSnapshot: (value) => notify((current) => current.onSnapshot?.(value)),
      onSnapshotError: (value) => notify((current) => current.onSnapshotError?.(value)),
      onEvent: (value) => notify((current) => current.onEvent?.(value)),
      onEvents: (value) => notify((current) => current.onEvents?.(value)),
      onConnectionRestored: (id) => notify((current) => current.onConnectionRestored?.(id)),
      onConnectionError: (id, error) => notify((current) => current.onConnectionError?.(id, error)),
    };
    const lease = getSharedAgentLiveSession(transport, sessionId).attach(
      listener,
      { live: liveSurface, snapshotView },
    );
    leaseRef.current = lease;
    return () => {
      if (leaseRef.current === lease) leaseRef.current = undefined;
      lease.release();
    };
  }, [sessionId, surfaceActive, transport]);

  useEffect(() => {
    leaseRef.current?.update({ live: liveSurface, snapshotView });
  }, [liveSurface, snapshotView]);

  return useCallback(
    (request?: AgentLiveSnapshotRequest) => (
      leaseRef.current?.loadSnapshot(request) ?? Promise.resolve(false)
    ),
    [],
  );
}

function getSharedAgentLiveSession(
  transport: ControlTransport,
  sessionId: string,
): SharedAgentLiveSession {
  let transportSessions = sharedAgentLiveSessions.get(transport);
  if (!transportSessions) {
    transportSessions = new Map();
    sharedAgentLiveSessions.set(transport, transportSessions);
  }
  const existing = transportSessions.get(sessionId);
  if (existing) return existing;
  let session: SharedAgentLiveSession;
  session = createSharedAgentLiveSession(transport, sessionId, () => {
    if (transportSessions?.get(sessionId) === session) transportSessions.delete(sessionId);
    if (transportSessions?.size === 0) sharedAgentLiveSessions.delete(transport);
  });
  transportSessions.set(sessionId, session);
  return session;
}

function createSharedAgentLiveSession(
  transport: ControlTransport,
  sessionId: string,
  onEmpty: () => void,
): SharedAgentLiveSession {
  const listeners = new Map<AgentLiveSessionCallbacks, ListenerState>();
  let active = false;
  let loading = false;
  let recoveryState: AgentRecoveryState = 'recovering';
  let snapshotAttempted = false;
  let loadedView: AgentSnapshotView | undefined;
  let latestSnapshot: AgentLiveSnapshot | undefined;
  let lastConnectionError: unknown;
  let connected = false;
  let snapshotController: AbortController | undefined;
  let snapshotGeneration = 0;
  let streamGeneration = 0;
  let unsubscribe: (() => void) | undefined;
  let snapshotNeedsRepair = false;
  let recoveryAttempt = 0;
  let recoveryTimer: ReturnType<typeof setTimeout> | undefined;

  const broadcast = (notify: (listener: AgentLiveSessionCallbacks) => void) => {
    for (const { listener } of listeners.values()) {
      try {
        notify(listener);
      } catch {
        // A view callback is a secondary consumer of the shared Runtime
        // projection. One broken/unmounting window must neither starve the
        // remaining windows nor escape through observer.next as a transport
        // failure that tears down the sole Session stream.
      }
    }
  };
  const setLoading = (next: boolean) => {
    loading = next;
    broadcast((listener) => listener.onLoadingChange?.(next));
  };
  const setRecoveryState = (next: AgentRecoveryState) => {
    recoveryState = next;
    broadcast((listener) => listener.onRecoveryState?.(next));
  };
  const clearRecoveryTimer = () => {
    if (recoveryTimer === undefined) return;
    clearTimeout(recoveryTimer);
    recoveryTimer = undefined;
  };
  const resetRecoveryBackoff = () => {
    recoveryAttempt = 0;
    clearRecoveryTimer();
  };
  const markConnectionStable = () => {
    // A heartbeat proves connectivity, not that a failed/gapped snapshot was
    // repaired. Do not cancel its recovery timer or show a false "synced".
    if (snapshotNeedsRepair || agentProjection(sessionId).needsSnapshot) return;
    resetRecoveryBackoff();
    if (connected && recoveryState === 'synced') return;
    connected = true;
    lastConnectionError = undefined;
    setRecoveryState('synced');
    broadcast((listener) => listener.onConnectionRestored?.(sessionId));
  };
  const scheduleAutomaticRecovery = (error?: unknown) => {
    if (!active || !shouldStream() || recoveryTimer !== undefined) return;
    const delayMs = ownerRecoveryDelayMs({
      ownerId: `agent:${sessionId}`,
      attempt: recoveryAttempt,
      baseDelayMs: AGENT_RECOVERY_BASE_DELAY_MS,
      maxDelayMs: AGENT_RECOVERY_MAX_DELAY_MS,
      retryAfterMs: retryAfterMsFromError(error),
    });
    recoveryAttempt += 1;
    setRecoveryState(
      recoveryAttempt >= AGENT_RECOVERY_VISIBLE_FAILURE_ATTEMPT
        ? 'failed'
        : 'recovering',
    );
    recoveryTimer = setTimeout(() => {
      recoveryTimer = undefined;
      if (!active || !shouldStream()) return;
      void loadSnapshot({
        preserveAfterSequence: agentProjection(sessionId).lastSequence,
      });
    }, delayMs);
  };
  const preferredSnapshotView = (): AgentSnapshotView => (
    [...listeners.values()].some(({ snapshotView }) => snapshotView === 'full')
      ? 'full'
      : 'recent'
  );
  const shouldStream = (): boolean => [...listeners.values()].some(({ live }) => live);
  const currentResumeToken = (): string => (
    agentProjection(sessionId).resumeToken
      || latestSnapshot?.resumeToken
      || ''
  );
  const clearStream = () => {
    streamGeneration += 1;
    const cancel = unsubscribe;
    unsubscribe = undefined;
    connected = false;
    batcher.clear();
    cancel?.();
  };
  const mergeSnapshotRequests = (
    current: AgentLiveSnapshotRequest | undefined,
    next: AgentLiveSnapshotRequest,
  ): AgentLiveSnapshotRequest => ({
    ...(current?.view === 'full' || next.view === 'full' || preferredSnapshotView() === 'full'
      ? { view: 'full' as const }
      : { view: 'recent' as const }),
    ...(
      current?.preserveAfterSequence === undefined && next.preserveAfterSequence === undefined
        ? {}
        : {
            preserveAfterSequence: Math.max(
              current?.preserveAfterSequence ?? -1,
              next.preserveAfterSequence ?? -1,
            ),
          }
    ),
  });
  const snapshotQueue = createSnapshotRequestQueue<AgentLiveSnapshotRequest>({
    merge: mergeSnapshotRequests,
    run: startSnapshot,
  });
  const scheduleSnapshotReload = (request: AgentLiveSnapshotRequest = {}) => {
    void loadSnapshot(request);
  };
  const batcher = createAgentDeltaBatcher((events) => {
    if (!active) return;
    const before = agentProjection(sessionId);
    const needsSnapshot = useAgentLiveStore.getState().applyEvents(sessionId, events);
    const after = agentProjection(sessionId);
    // A batch is text deltas or one non-delta event. Only its committed prefix
    // may notify views; duplicates, foreign events and the gap-causing suffix
    // must not open tool windows or schedule terminal refreshes.
    let notifiedSequence = before.lastSequence;
    const committed = events.filter((event) => {
      if (event.sessionId !== sessionId
        || (before.needsSnapshot && event.eventType !== 'snapshot')
        || event.sequence <= notifiedSequence
        || event.sequence > after.lastSequence) return false;
      notifiedSequence = event.sequence;
      return true;
    });
    if (needsSnapshot) {
      setRecoveryState('recovering');
      scheduleSnapshotReload({ preserveAfterSequence: after.lastSequence });
    }
    for (const event of committed) {
      broadcast((listener) => listener.onEvent?.(event));
    }
    if (committed.length) broadcast((listener) => listener.onEvents?.(committed));
  });

  function requestSnapshotValue(
    view: AgentSnapshotView,
    signal: AbortSignal,
  ): Promise<unknown> {
    return transport.request({
      pathId: 'agent.session.snapshot',
      params: { sessionId },
      ...(view === 'recent' ? { query: { view: 'recent' as const } } : {}),
      signal,
    });
  }

  async function performSnapshot(
    request: AgentLiveSnapshotRequest,
    requestId: number,
    controller: AbortController,
  ): Promise<boolean> {
    let requestedView = request.view ?? preferredSnapshotView();
    let value: unknown = undefined;
    try {
      while (true) {
        try {
          value = await requestSnapshotValue(requestedView, controller.signal);
        } catch (error) {
          if (requestedView === 'recent' && preferredSnapshotView() === 'full') {
            requestedView = 'full';
            continue;
          }
          throw error;
        }
        if (!isCurrentSnapshot(requestId, controller)) return false;
        if (requestedView === 'recent' && preferredSnapshotView() === 'full') {
          requestedView = 'full';
          continue;
        }
        break;
      }
      if (!isCurrentSnapshot(requestId, controller)) return false;
      const recent = isRecentAgentSnapshot(value);
      const actualView: AgentSnapshotView = recent ? 'recent' : 'full';
      const presentable = actualView === 'full' || recentAgentSnapshotIsPresentable(value);
      const sequence = agentSnapshotSequence(value);
      const resumeToken = agentSnapshotResumeToken(value);
      const projectionBeforeHydration = agentProjection(sessionId);
      // Equality is authoritative only for a quiescent snapshot. A stale busy
      // snapshot at the same cursor must not overwrite a terminal SSE event;
      // idle/quiescent metadata at that cursor may settle activity left behind
      // by a dropped stream.
      const equalCursorIsQuiescent = request.preserveAfterSequence !== undefined
        && sequence === request.preserveAfterSequence
        && isRecord(value)
        && (value.runtimeQuiescent === true || value.partial !== true)
        && typeof value.status === 'string'
        && ['idle', 'ready', 'stopped', 'active'].includes(value.status);
      // An explicit replay gap is different from an ordinary reconnect: the
      // reducer has already fenced every following event until a snapshot
      // clears `needsSnapshot`. If the server has no newer durable event, its
      // equal-cursor snapshot is still the only authoritative repair, including
      // while the Runtime remains busy.
      const equalCursorRepairsGap = projectionBeforeHydration.needsSnapshot
        && sequence === projectionBeforeHydration.lastSequence;
      const retainNewerTerminal = presentable
        && equalCursorRepairsGap
        && isTerminalAgentProjection(projectionBeforeHydration)
        && isBusyAgentSnapshot(value);
      const shouldHydrate = presentable
        && !retainNewerTerminal
        && (
          request.preserveAfterSequence === undefined
          || sequence > request.preserveAfterSequence
          || equalCursorIsQuiescent
          || equalCursorRepairsGap
        );
      const hydrated = shouldHydrate
        && useAgentLiveStore.getState().hydrate(sessionId, value);
      const repairedWithoutRegression = retainNewerTerminal
        && clearEqualCursorGap(sessionId, sequence, resumeToken);
      const snapshot = {
        sessionId,
        value,
        view: actualView,
        presentable,
        hydrated: hydrated || repairedWithoutRegression,
        sequence,
        resumeToken,
      };
      snapshotAttempted = true;
      snapshotNeedsRepair = !presentable || agentProjection(sessionId).needsSnapshot;
      if (snapshot.hydrated) {
        loadedView = actualView;
        latestSnapshot = snapshot;
      }
      setLoading(false);
      // A transport success is not a store commit. Rejected stale/partial
      // responses cannot mark the view loaded or overwrite its metadata.
      if (snapshot.hydrated) broadcast((listener) => listener.onSnapshot?.(snapshot));
      else if (!presentable) broadcast((listener) => listener.onSnapshotError?.({
        sessionId,
        view: actualView,
        error: new Error('返回的记录不完整，请加载完整记录。'),
        recoverable: actualView === 'recent',
      }));
      if (shouldStream()) maybeSubscribe();
      else if (!snapshotNeedsRepair) setRecoveryState('synced');
      if (snapshotNeedsRepair) scheduleAutomaticRecovery();
      return snapshot.hydrated;
    } catch (error) {
      if (!isCurrentSnapshot(requestId, controller) || isAbortError(error)) return false;
      snapshotAttempted = true;
      snapshotNeedsRepair = true;
      const recoverable = requestedView === 'recent' && preferredSnapshotView() !== 'full';
      const failure = {
        sessionId,
        view: requestedView,
        error,
        recoverable,
      };
      setLoading(false);
      setRecoveryState('failed');
      broadcast((listener) => listener.onSnapshotError?.(failure));
      const resumeStream = recoverable && shouldStream();
      if (resumeStream) maybeSubscribe();
      if (shouldStream()) scheduleAutomaticRecovery(error);
      return false;
    }
  }

  function isCurrentSnapshot(requestId: number, controller: AbortController): boolean {
    return active && requestId === snapshotGeneration && !controller.signal.aborted;
  }

  function startSnapshot(request: AgentLiveSnapshotRequest): Promise<boolean> {
    if (!active) return Promise.resolve(false);
    clearRecoveryTimer();
    // Commit the already-received delta tail before cutting over to a read.
    // Otherwise a snapshot that the store rejects can silently lose this tail.
    batcher.flush();
    if (!active) return Promise.resolve(false);
    clearStream();
    const requestId = ++snapshotGeneration;
    const controller = new AbortController();
    snapshotController = controller;
    setRecoveryState('recovering');
    setLoading(true);
    return performSnapshot(request, requestId, controller).finally(() => {
      if (snapshotController === controller) snapshotController = undefined;
    });
  }

  function loadSnapshot(request: AgentLiveSnapshotRequest = {}): Promise<boolean> {
    if (!active) return Promise.resolve(false);
    // Calls made during a read are invalidations, not subscribers to the old
    // answer. Merge them into one trailing read and resolve after THAT read.
    return snapshotQueue.request({
      ...request,
      view: request.view ?? preferredSnapshotView(),
    });
  }

  function maybeSubscribe(): void {
    if (!active || !shouldStream() || !snapshotAttempted || unsubscribe) return;
    const subscriptionGeneration = ++streamGeneration;
    if (!connected) setRecoveryState('recovering');
    try {
      const cancel = transport.subscribe<UiAgentEvent>(
        {
          pathId: 'agent.session.events',
          params: { sessionId },
          lastEventId: currentResumeToken(),
        },
        {
          stable: () => {
            if (!active || subscriptionGeneration !== streamGeneration) return;
            markConnectionStable();
          },
          next: (event) => {
            if (!active || subscriptionGeneration !== streamGeneration || event.sessionId !== sessionId) return;
            if (event.eventType === 'snapshot_required') {
              batcher.flush();
              // `snapshot_required` is a transient recovery control. The
              // backend intentionally gives it `currentSequence + 1` without
              // advancing the durable journal, so using the control sequence
              // as the preservation fence makes the authoritative snapshot at
              // `currentSequence` look stale and reconnects from the old
              // cursor forever. Fence only the durable projection that was
              // actually applied before this control arrived.
              const preserveAfterSequence = agentProjection(sessionId).lastSequence;
              const needsSnapshot = useAgentLiveStore.getState().applyEvents(sessionId, [event]);
              broadcast((listener) => listener.onEvent?.(event));
              if (needsSnapshot) {
                setRecoveryState('recovering');
                scheduleSnapshotReload({ preserveAfterSequence });
              }
              return;
            }
            batcher.push(event);
            // A listener can release the last lease while handling a terminal.
            if (active && subscriptionGeneration === streamGeneration) markConnectionStable();
          },
          error: (error) => {
            if (!active || subscriptionGeneration !== streamGeneration) return;
            unsubscribe?.();
            unsubscribe = undefined;
            streamGeneration += 1;
            connected = false;
            lastConnectionError = error;
            setRecoveryState('recovering');
            broadcast((listener) => listener.onConnectionError?.(sessionId, error));
            scheduleAutomaticRecovery(error);
          },
        },
      );
      if (active && subscriptionGeneration === streamGeneration) unsubscribe = cancel;
      else cancel();
    } catch (error) {
      if (!active || subscriptionGeneration !== streamGeneration) return;
      connected = false;
      lastConnectionError = error;
      setRecoveryState('recovering');
      broadcast((listener) => listener.onConnectionError?.(sessionId, error));
      scheduleAutomaticRecovery(error);
    }
  }

  function reconcileOptions(): void {
    if (!active) return;
    if (!shouldStream()) {
      if (unsubscribe) clearStream();
    } else if (snapshotAttempted) {
      maybeSubscribe();
    }
    if (preferredSnapshotView() === 'full' && loadedView !== 'full' && !snapshotQueue.busy) {
      void loadSnapshot({ view: 'full' });
    }
  }

  function stop(): void {
    active = false;
    clearRecoveryTimer();
    snapshotGeneration += 1;
    snapshotController?.abort();
    snapshotController = undefined;
    clearStream();
    batcher.clear();
    snapshotQueue.close();
    snapshotNeedsRepair = false;
    loading = false;
    snapshotAttempted = false;
    loadedView = undefined;
    latestSnapshot = undefined;
    lastConnectionError = undefined;
  }

  return {
    attach(listener, options) {
      const alreadyRunning = active;
      listeners.set(listener, { listener, ...options });
      if (!alreadyRunning) {
        active = true;
        useAgentLiveStore.getState().ensure(sessionId);
        void loadSnapshot({ view: preferredSnapshotView() });
      } else {
        // A second window needs the accepted snapshot notification, not a
        // second fetch/stream or a replay that rehydrates the shared store.
        const notify = (call: () => void) => { try { call(); } catch { /* view only */ } };
        notify(() => listener.onLoadingChange?.(loading));
        notify(() => listener.onRecoveryState?.(recoveryState));
        if (latestSnapshot) notify(() => listener.onSnapshot?.(latestSnapshot!));
        if (lastConnectionError !== undefined) notify(() => listener.onConnectionError?.(sessionId, lastConnectionError));
        else if (connected) notify(() => listener.onConnectionRestored?.(sessionId));
        reconcileOptions();
      }
      let released = false;
      return {
        loadSnapshot,
        update(nextOptions) {
          if (released || !listeners.has(listener)) return;
          const previousView = preferredSnapshotView();
          const previousLive = shouldStream();
          listeners.set(listener, { listener, ...nextOptions });
          const nextView = preferredSnapshotView();
          const nextLive = shouldStream();
          if (previousLive !== nextLive || previousView !== nextView) reconcileOptions();
        },
        release() {
          if (released) return;
          released = true;
          const previousLive = shouldStream();
          listeners.delete(listener);
          if (listeners.size > 0) {
            if (previousLive !== shouldStream()) reconcileOptions();
            return;
          }
          stop();
          onEmpty();
        },
      };
    },
  };
}

export function isRecentAgentSnapshot(value: unknown): boolean {
  return isRecord(value)
    && value.snapshotScope === 'recent'
    && value.partial === true;
}

export function recentAgentSnapshotIsPresentable(value: unknown): boolean {
  if (!isRecord(value)) return false;
  const status = typeof value.status === 'string' ? value.status : '';
  if (status === 'active' || status === 'busy') return true;
  const items = Array.isArray(value.items)
    ? value.items
    : Array.isArray(value.messages)
      ? value.messages
      : [];
  const last = items.at(-1);
  return !(isRecord(last) && last.role === 'user');
}

function agentSnapshotSequence(value: unknown): number {
  if (!isRecord(value)) return -1;
  return typeof value.lastSequence === 'number' && Number.isFinite(value.lastSequence)
    ? value.lastSequence
    : -1;
}

function agentSnapshotResumeToken(value: unknown): string {
  if (!isRecord(value)) return '';
  return typeof value.resumeToken === 'string'
    ? value.resumeToken
    : typeof value.lastEventId === 'string'
      ? value.lastEventId
      : '';
}

function isTerminalAgentProjection(projection: ReturnType<typeof agentProjection>): boolean {
  if (!['idle', 'ready', 'stopped', 'active', 'failed', 'faulted'].includes(projection.status)) {
    return false;
  }
  return !projection.turnOrder.some((turnId) => (
    ['queued', 'running', 'waiting'].includes(projection.turnsById[turnId]?.status ?? '')
  ));
}

function isBusyAgentSnapshot(value: unknown): boolean {
  if (!isRecord(value) || typeof value.status !== 'string') return false;
  return ['busy', 'working', 'waiting', 'aborting', 'stopping'].includes(value.status);
}

function clearEqualCursorGap(
  sessionId: string,
  sequence: number,
  resumeToken: string,
): boolean {
  let repaired = false;
  useAgentLiveStore.setState((state) => {
    const current = state.projections[sessionId];
    if (
      !current
      || !current.needsSnapshot
      || current.lastSequence !== sequence
      || !isTerminalAgentProjection(current)
    ) return state;
    repaired = true;
    const nextResumeToken = resumeToken || current.resumeToken;
    return {
      projections: {
        ...state.projections,
        [sessionId]: {
          ...current,
          lastEventId: nextResumeToken || current.lastEventId,
          resumeToken: nextResumeToken,
          needsSnapshot: false,
          gap: undefined,
        },
      },
    };
  });
  return repaired;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isAbortError(value: unknown): boolean {
  return value instanceof DOMException && value.name === 'AbortError';
}
