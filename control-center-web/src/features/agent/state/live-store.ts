import { create } from 'zustand';
import {
  applyAgentBackgroundJobReceipt,
  acknowledgeOptimisticAgentMessage,
  abortAgentTurn,
  agentSnapshotFromResponse,
  appendOptimisticAgentMessage,
  applyAgentSnapshot,
  durableRecoveryFromSnapshot,
  mergeAgentSnapshotHistory,
  createAgentProjection,
  discardOptimisticAgentMessage,
  failOptimisticAgentMessage,
  requeueOptimisticAgentMessage,
  reduceAgentEvents,
  rewriteOptimisticAgentMessage,
  type AgentProjectionState,
  type AgentSnapshot,
} from '@/contracts/agent-reducer';
import type { UiAgentEvent } from '@/contracts/ui-events';
import type { ControlTransport } from '@/platform/transport';
import { controlTransportScopeKey } from '@/platform/transport-scope';

type AgentLiveProjection = AgentProjectionState & {
  recoveryCursor?: number;
  /** Default idle and partial history alone are not terminal confirmation. */
  retentionConfirmed?: boolean;
};

const INACTIVE_SETTLED_PROJECTION_LIMIT = 24;
// One pin per existing shared live owner, whose listener map owns lease counts.
// Only keys are held here; releasing the last listener removes the pin.
const liveOwnerProjectionKeys = new Set<string>();

/** In-memory identity follows the existing live owner, not its endpoint URL. */
export type AgentSessionAddress = Readonly<{ sessionId: string; projectionKey: string }>;
export type AgentSessionTarget = AgentSessionAddress | string;
const transportAddresses = new WeakMap<ControlTransport, { prefix: string; sessions: Map<string, AgentSessionAddress> }>();

export function agentSessionAddress(transport: ControlTransport | null | undefined, sessionId: string): AgentSessionTarget {
  // Explicit provider-less renderer/store fixtures retain their old namespace.
  // A scoped read never consults it, even when it contains the same Session ID.
  if (!transport) return sessionId;
  let scope = transportAddresses.get(transport);
  if (!scope) {
    scope = { prefix: controlTransportScopeKey(transport), sessions: new Map() };
    transportAddresses.set(transport, scope);
  }
  let address = scope.sessions.get(sessionId);
  if (!address) {
    address = Object.freeze({ sessionId, projectionKey: JSON.stringify([scope.prefix, sessionId]) });
    scope.sessions.set(sessionId, address);
  }
  return address;
}

export function agentProjectionKey(target: AgentSessionTarget): string {
  return typeof target === 'string' ? target : target.projectionKey;
}
function protocolSessionId(target: AgentSessionTarget): string {
  return typeof target === 'string' ? target : target.sessionId;
}
export function selectAgentProjection(state: Pick<AgentLiveStore, 'projections'>, target: AgentSessionTarget): AgentLiveProjection | undefined {
  return state.projections[agentProjectionKey(target)];
}

/** Shared by task summaries and the Session composer; old turns cannot revive work. */
export function latestActiveAgentTurnId(projection?: AgentProjectionState): string {
  if (!projection) return '';
  if (projection.status === 'retrying') {
    for (const id of [...projection.activityOrder].reverse()) {
      const activity = projection.activitiesById[id];
      if (activity?.payload.phase === 'provider_retry' && activity.status === 'running'
        && projection.turnsById[activity.turnId]?.status === 'running') return activity.turnId;
    }
  }
  for (let index = projection.turnOrder.length - 1; index >= 0; index -= 1) {
    const turnId = projection.turnOrder[index] ?? '';
    const turn = projection.turnsById[turnId];
    if (!turn || (turn.messageIds.length === 0 && turn.activityIds.length === 0)) continue;
    if (!turn.messageIds.length && turn.activityIds.every(id => projection.activitiesById[id]?.kind === 'context_compaction')) return '';
    return ['queued', 'running', 'waiting'].includes(turn.status) ? turnId : '';
  }
  return '';
}
interface AgentSnapshotHydrationOptions {
  /** Only a read started by the recovery owner after this control may rewind. */
  recoveryCursor?: number;
  /** Live owner's cursor when the read began; newer events retain control authority. */
  controlMetadataSequence?: number;
}

interface AgentLiveStore {
  projections: Record<string, AgentLiveProjection>;
  setLiveOwnerActive(target: AgentSessionTarget, active: boolean): void;
  ensure(target: AgentSessionTarget): void;
  hydrate(target: AgentSessionTarget, value: unknown, options?: AgentSnapshotHydrationOptions): boolean;
  hydrateSnapshot(target: AgentSessionTarget, snapshot: AgentSnapshot, options?: AgentSnapshotHydrationOptions): boolean;
  applyEvents(target: AgentSessionTarget, events: readonly UiAgentEvent[]): boolean;
  applyBackgroundJobReceipt(target: AgentSessionTarget, receipt: unknown): boolean;
  appendOptimistic(
    target: AgentSessionTarget,
    input: {
      clientMessageId: string;
      retryOfClientMessageId?: string;
      text: string;
      attachments?: string[];
      nowMs: number;
      turnId?: string;
      delivery?: 'prompt' | 'steer' | 'followUp';
    },
  ): void;
  rewriteOptimistic(
    target: AgentSessionTarget,
    targetMessageId: string,
    input: {
      clientMessageId: string;
      text: string;
      attachments?: string[];
      nowMs: number;
    },
  ): void;
  discardOptimistic(target: AgentSessionTarget, clientMessageId: string): void;
  failOptimistic(
    target: AgentSessionTarget,
    clientMessageId: string,
    error: string,
    nowMs: number,
    admissionState?: 'ambiguous' | 'pending' | 'unresolved',
  ): void;
  requeueOptimistic(
    target: AgentSessionTarget,
    clientMessageId: string,
    nowMs: number,
  ): void;
  acknowledgeOptimistic(target: AgentSessionTarget, clientMessageId: string, nowMs: number): void;
  abortTurn(target: AgentSessionTarget, turnId: string, nowMs: number): void;
  clear(target: AgentSessionTarget): void;
}

export const useAgentLiveStore = create<AgentLiveStore>((set, get) => ({
  projections: {},
  setLiveOwnerActive(target, active) {
    const key = agentProjectionKey(target);
    if (active) {
      liveOwnerProjectionKeys.add(key);
      return;
    }
    liveOwnerProjectionKeys.delete(key);
    set((state) => {
      const projection = state.projections[key];
      // Last release counts as recent use, including a temporarily hidden page.
      const projections = { ...state.projections };
      delete projections[key];
      if (projection) projections[key] = projection;
      return { projections: projection && canEvictProjection(projection) ? pruneInactiveProjections(projections) : projections };
    });
  },
  ensure(target) {
    const sessionId = protocolSessionId(target); const key = agentProjectionKey(target);
    if (!sessionId || get().projections[key]) return;
    set((state) => ({
      projections: updateProjection(state.projections, key, createAgentProjection(sessionId)),
    }));
  },
  hydrate(target, value, options) {
    const sessionId = protocolSessionId(target);
    if (isRecord(value) && (
      value.ok === false
      || (typeof value.sessionId === 'string' && value.sessionId !== sessionId)
    )) return false;
    return get().hydrateSnapshot(target, agentSnapshotFromResponse(value), options);
  },
  hydrateSnapshot(target, snapshot, options) {
    const sessionId = protocolSessionId(target); const key = agentProjectionKey(target);
    if (snapshot.sessionId !== undefined && snapshot.sessionId !== sessionId) return false;
    const current: AgentLiveProjection = get().projections[key] ?? createAgentProjection(sessionId);
    const recoveryCursor = current.recoveryCursor;
    if (current.needsSnapshot && recoveryCursor !== undefined && snapshot.lastSequence < recoveryCursor) return false;
    const reset = current.needsSnapshot && recoveryCursor !== undefined && recoveryCursor < current.lastSequence;
    if (reset && options?.recoveryCursor !== recoveryCursor) return false;
    if (options?.controlMetadataSequence !== undefined && options.controlMetadataSequence !== current.lastSequence) {
      snapshot = { ...snapshot, projectionCurrent: false };
    }
    if (snapshot.lastSequence < current.lastSequence && !reset) {
      let projection = mergeAgentSnapshotHistory(current, normalizeLegacyHistoryTurns(snapshot));
      // History pagination owns transcript rows, not the native control cursor.
      // A fresh owner read may carry current compaction metadata beside older
      // rows; a delayed read cannot overwrite any event received since it began.
      const recovery = durableRecoveryFromSnapshot(snapshot, sessionId);
      if (options?.controlMetadataSequence === current.lastSequence && recovery
        && recovery.compactionTarget !== undefined) {
        projection = { ...projection, durableRecovery: recovery, runtimeEngine: 'durable' };
      }
      if (projection === current) return false;
      set((state) => ({ projections: updateProjection(state.projections, key, projection) }));
      return true;
    }
    // A snapshot with no messages can only be a transient/partial projection
    // failure for a Session that already has durable history. Rebuild the
    // cursor and terminal/status metadata against the last confirmed
    // messages, then retain prior activity rows that this empty response
    // cannot disprove.
    // This keeps an idle terminal snapshot able to clear a stale spinner
    // without allowing a successful-looking empty response to erase text.
    const preserveHistory = (
      !reset && current.messageOrder.length > 0
      && snapshot.messages.length === 0
    );
    const hydratedSnapshot = preserveHistory
      ? {
          ...snapshot,
          messages: current.messageOrder
            .map((messageId) => current.messagesById[messageId])
            .filter((message): message is NonNullable<typeof message> => Boolean(message) && !message.id.startsWith('local:')),
        }
      : snapshot;
    const projection = applyAgentSnapshot(reset ? optimisticResetProjection(current) : current, normalizeLegacyHistoryTurns(hydratedSnapshot), {
      preserveConfirmedActivities: preserveHistory,
    });
    // Several call sites can request a snapshot outside the shared live owner.
    // A response captured before an SSE terminal may therefore arrive later
    // with the same durable cursor but an older busy/live tail. Sequence
    // equality cannot supersede a terminal projection unless an explicit gap
    // has fenced the store and made that snapshot the recovery authority.
    if (
      snapshot.lastSequence === current.lastSequence
      && current.lastSequence > 0
      && !current.needsSnapshot
      && isTerminalProjection(current)
      && !isTerminalProjection(projection)
    ) {
      // A completed user turn cannot invalidate unrelated native compaction.
      // Preserve its terminal transcript while accepting current control state.
      const recovery = durableRecoveryFromSnapshot(snapshot, sessionId);
      if (!recovery?.compactionTarget && !current.durableRecovery?.compactionTarget) return false;
      if (!recovery || current.durableRecovery?.compactionTarget && recovery.compactionTarget === undefined) return false;
      set((state) => ({ projections: updateProjection(state.projections, key, {
        ...current, durableRecovery: recovery, runtimeEngine: projection.runtimeEngine,
      }) }));
      return true;
    }
    set((state) => ({
      projections: updateProjection(state.projections, key, projection, snapshotConfirmsQuiescence(snapshot)),
    }));
    return true;
  },
  applyEvents(target, events) {
    const sessionId = protocolSessionId(target); const key = agentProjectionKey(target);
    const current = get().projections[key] ?? createAgentProjection(sessionId);
    let projection: AgentLiveProjection = reduceAgentEvents(current, events);
    for (const event of events) {
      if (event.sessionId !== sessionId || event.eventType !== 'snapshot_required') continue;
      const prefix = `${sessionId}:snapshot-required:`;
      const suffix = event.resumeToken.startsWith(prefix) ? event.resumeToken.slice(prefix.length) : '';
      const cursor = /^\d+$/.test(suffix) ? Number(suffix) : NaN;
      if (Number.isSafeInteger(cursor) && cursor >= 0 && event.sequence === cursor + 1) {
        projection = { ...projection, recoveryCursor: cursor };
      }
    }
    if (projection === current) return current.needsSnapshot;
    const terminalConfirmed = events.some((event) => event.sessionId === sessionId
      && event.sequence > current.lastSequence && event.sequence <= projection.lastSequence
      && ((event.eventType === 'turn_completed' || event.eventType === 'turn_failed') && Boolean(event.turnId)
        || event.eventType === 'status_changed' && isTerminalStatus(event.payload.status)
        || event.eventType === 'snapshot' && snapshotConfirmsQuiescence(agentSnapshotFromResponse(
          event.payload.snapshot ?? event.payload,
        ))));
    set((state) => ({
      projections: updateProjection(state.projections, key, projection, terminalConfirmed || undefined),
    }));
    return projection.needsSnapshot;
  },
  applyBackgroundJobReceipt(target, receipt) {
    const sessionId = protocolSessionId(target); const key = agentProjectionKey(target);
    const current = get().projections[key] ?? createAgentProjection(sessionId);
    const projection = applyAgentBackgroundJobReceipt(current, receipt);
    if (projection === current) return false;
    set((state) => ({
      projections: updateProjection(state.projections, key, projection),
    }));
    return true;
  },
  appendOptimistic(target, input) {
    const sessionId = protocolSessionId(target); const key = agentProjectionKey(target);
    const current = get().projections[key] ?? createAgentProjection(sessionId);
    const projection = appendOptimisticAgentMessage(current, input);
    set((state) => ({
      projections: updateProjection(state.projections, key, projection),
    }));
  },
  rewriteOptimistic(target, targetMessageId, input) {
    const sessionId = protocolSessionId(target); const key = agentProjectionKey(target);
    const current = get().projections[key] ?? createAgentProjection(sessionId);
    const projection = rewriteOptimisticAgentMessage(current, targetMessageId, input);
    set((state) => ({
      projections: updateProjection(state.projections, key, projection),
    }));
  },
  discardOptimistic(target, clientMessageId) {
    const key = agentProjectionKey(target);
    const current = get().projections[key];
    if (!current) return;
    const projection = discardOptimisticAgentMessage(current, clientMessageId);
    set((state) => ({
      projections: updateProjection(state.projections, key, projection),
    }));
  },
  failOptimistic(
    target,
    clientMessageId,
    error,
    nowMs,
    admissionState,
  ) {
    const key = agentProjectionKey(target);
    const current = get().projections[key];
    if (!current) return;
    const projection = failOptimisticAgentMessage(
      current,
      clientMessageId,
      error,
      nowMs,
      admissionState,
    );
    set((state) => ({
      projections: updateProjection(state.projections, key, projection),
    }));
  },
  requeueOptimistic(target, clientMessageId, nowMs) {
    const key = agentProjectionKey(target);
    const current = get().projections[key];
    if (!current) return;
    const projection = requeueOptimisticAgentMessage(
      current,
      clientMessageId,
      nowMs,
    );
    set((state) => ({
      projections: updateProjection(state.projections, key, projection),
    }));
  },
  acknowledgeOptimistic(target, clientMessageId, nowMs) {
    const key = agentProjectionKey(target);
    const current = get().projections[key];
    if (!current) return;
    const projection = acknowledgeOptimisticAgentMessage(
      current,
      clientMessageId,
      nowMs,
    );
    if (projection === current) return;
    set((state) => ({
      projections: updateProjection(state.projections, key, projection),
    }));
  },
  abortTurn(target, turnId, nowMs) {
    const key = agentProjectionKey(target);
    const current = get().projections[key];
    if (!current) return;
    const projection = abortAgentTurn(current, turnId, nowMs);
    set((state) => ({
      projections: updateProjection(state.projections, key, projection),
    }));
  },
  clear(target) {
    set((state) => {
      const projections = { ...state.projections };
      delete projections[agentProjectionKey(target)];
      return { projections };
    });
  },
}));

export function agentProjection(target: AgentSessionTarget): AgentLiveProjection {
  return (
    selectAgentProjection(useAgentLiveStore.getState(), target) ?? createAgentProjection(protocolSessionId(target))
  );
}

function updateProjection(
  current: Record<string, AgentLiveProjection>,
  key: string,
  projection: AgentLiveProjection,
  terminalConfirmed?: boolean,
): Record<string, AgentLiveProjection> {
  const previous = current[key];
  const projections = { ...current };
  // Scoped keys are non-integer strings: insertion order is the LRU order.
  delete projections[key];
  projections[key] = {
    ...projection,
    retentionConfirmed: isTerminalProjection(projection)
      && (terminalConfirmed ?? (previous?.retentionConfirmed === true && isTerminalProjection(previous))),
  };
  // Streaming an observed Session cannot add an inactive cache entry. Avoid
  // rescanning retained transcripts on the hot delta path.
  return !liveOwnerProjectionKeys.has(key) && canEvictProjection(projections[key])
    ? pruneInactiveProjections(projections)
    : projections;
}

/** Mutation-triggered cache only: no timers and no unconditional release clear.
 * Active/unknown/pending projections deliberately remain outside this bound;
 * dropping their admission or recovery identity would be a correctness bug. */
function pruneInactiveProjections(projections: Record<string, AgentLiveProjection>): Record<string, AgentLiveProjection> {
  const eligible = Object.keys(projections).filter((key) => (
    !liveOwnerProjectionKeys.has(key) && canEvictProjection(projections[key])
  ));
  for (const key of eligible.slice(0, Math.max(0, eligible.length - INACTIVE_SETTLED_PROJECTION_LIMIT))) {
    delete projections[key];
  }
  return projections;
}

function canEvictProjection(projection: AgentLiveProjection): boolean {
  const recovery = projection.durableRecovery;
  return projection.retentionConfirmed === true
    && isTerminalProjection(projection) && !projection.needsSnapshot
    && Object.keys(projection.optimisticByClientMessageId).length === 0
    && !recovery?.paused && !recovery?.recoverable && !recovery?.activeTurn && !recovery?.compactionTarget
    && projection.messageQueue.steering.length === 0 && projection.messageQueue.followUp.length === 0
    && !Object.values(projection.messagesById).some((message) => message.admissionState
      || ['queued', 'streaming'].includes(message.status)
      || message.deliveryState === 'sending' || message.deliveryState === 'accepted')
    && !Object.values(projection.activitiesById).some((activity) => ['running', 'waiting'].includes(activity.status))
    && Object.values(projection.backgroundJobsById).every((job) => ['completed', 'failed', 'cancelled'].includes(job.status));
}

function snapshotConfirmsQuiescence(snapshot: AgentSnapshot): boolean {
  return snapshot.projectionCurrent !== false
    && (snapshot.runtimeQuiescent === true || !snapshot.partial) && isTerminalStatus(snapshot.status);
}

function isTerminalStatus(status: unknown): boolean {
  return typeof status === 'string' && ['idle', 'ready', 'stopped', 'active', 'failed', 'faulted'].includes(status);
}

/** A reset replaces old confirmed history, while an unresolved local admission
 * still belongs to its command receipt and must remain recoverable. */
function optimisticResetProjection(current: AgentProjectionState): AgentProjectionState {
  const next = createAgentProjection(current.sessionId);
  for (const [clientMessageId, messageId] of Object.entries(current.optimisticByClientMessageId)) {
    const message = current.messagesById[messageId];
    if (!message) continue;
    next.optimisticByClientMessageId[clientMessageId] = messageId;
    next.messagesById[messageId] = message;
    next.messageOrder.push(messageId);
    const turn = current.turnsById[message.turnId];
    if (turn && !next.turnsById[turn.id]) {
      next.turnsById[turn.id] = turn;
      next.turnOrder.push(turn.id);
    }
  }
  return next;
}

function normalizeLegacyHistoryTurns(snapshot: AgentSnapshot): AgentSnapshot {
  let currentTurnId = '';
  let changed = false;
  const messages = snapshot.messages.map((rawMessage, index) => {
    if (!isRecord(rawMessage) || rawMessage.turnId !== 'history') {
      currentTurnId = '';
      return rawMessage;
    }
    const role = typeof rawMessage.role === 'string' ? rawMessage.role : '';
    if (role === 'user' || !currentTurnId) {
      const messageId = typeof rawMessage.id === 'string' && rawMessage.id
        ? rawMessage.id
        : String(index);
      currentTurnId = `history:${messageId}`;
    }
    changed = true;
    return { ...rawMessage, turnId: currentTurnId };
  });
  return changed ? { ...snapshot, messages } : snapshot;
}


function isTerminalProjection(projection: AgentProjectionState): boolean {
  if (!isTerminalStatus(projection.status)) {
    return false;
  }
  return !projection.turnOrder.some((turnId) => (
    ['queued', 'running', 'waiting'].includes(projection.turnsById[turnId]?.status ?? '')
  ));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
