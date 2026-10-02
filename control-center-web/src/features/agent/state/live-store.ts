import { create } from 'zustand';
import {
  applyAgentBackgroundJobReceipt,
  acknowledgeOptimisticAgentMessage,
  abortAgentTurn,
  agentSnapshotFromResponse,
  appendOptimisticAgentMessage,
  applyAgentSnapshot,
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

type AgentLiveProjection = AgentProjectionState & { recoveryCursor?: number };
interface AgentSnapshotHydrationOptions {
  /** Only a read started by the recovery owner after this control may rewind. */
  recoveryCursor?: number;
}

interface AgentLiveStore {
  projections: Record<string, AgentLiveProjection>;
  ensure(sessionId: string): void;
  hydrate(sessionId: string, value: unknown, options?: AgentSnapshotHydrationOptions): boolean;
  hydrateSnapshot(sessionId: string, snapshot: AgentSnapshot, options?: AgentSnapshotHydrationOptions): boolean;
  applyEvents(sessionId: string, events: readonly UiAgentEvent[]): boolean;
  applyBackgroundJobReceipt(sessionId: string, receipt: unknown): boolean;
  appendOptimistic(
    sessionId: string,
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
    sessionId: string,
    targetMessageId: string,
    input: {
      clientMessageId: string;
      text: string;
      attachments?: string[];
      nowMs: number;
    },
  ): void;
  discardOptimistic(sessionId: string, clientMessageId: string): void;
  failOptimistic(
    sessionId: string,
    clientMessageId: string,
    error: string,
    nowMs: number,
    admissionState?: 'ambiguous' | 'pending' | 'unresolved',
  ): void;
  requeueOptimistic(
    sessionId: string,
    clientMessageId: string,
    nowMs: number,
  ): void;
  acknowledgeOptimistic(sessionId: string, clientMessageId: string, nowMs: number): void;
  abortTurn(sessionId: string, turnId: string, nowMs: number): void;
  clear(sessionId: string): void;
}

export const useAgentLiveStore = create<AgentLiveStore>((set, get) => ({
  projections: {},
  ensure(sessionId) {
    if (!sessionId || get().projections[sessionId]) return;
    set((state) => ({
      projections: {
        ...state.projections,
        [sessionId]: createAgentProjection(sessionId),
      },
    }));
  },
  hydrate(sessionId, value, options) {
    if (isRecord(value) && (
      value.ok === false
      || (typeof value.sessionId === 'string' && value.sessionId !== sessionId)
    )) return false;
    return get().hydrateSnapshot(sessionId, agentSnapshotFromResponse(value), options);
  },
  hydrateSnapshot(sessionId, snapshot, options) {
    const current: AgentLiveProjection = get().projections[sessionId] ?? createAgentProjection(sessionId);
    const recoveryCursor = current.recoveryCursor;
    if (current.needsSnapshot && recoveryCursor !== undefined && snapshot.lastSequence < recoveryCursor) return false;
    const reset = current.needsSnapshot && recoveryCursor !== undefined && recoveryCursor < current.lastSequence;
    if (reset && options?.recoveryCursor !== recoveryCursor) return false;
    if (snapshot.lastSequence < current.lastSequence && !reset) {
      const projection = mergeAgentSnapshotHistory(current, normalizeLegacyHistoryTurns(snapshot));
      if (projection === current) return false;
      set((state) => ({ projections: { ...state.projections, [sessionId]: projection } }));
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
    ) return false;
    set((state) => ({
      projections: { ...state.projections, [sessionId]: projection },
    }));
    return true;
  },
  applyEvents(sessionId, events) {
    const current = get().projections[sessionId] ?? createAgentProjection(sessionId);
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
    set((state) => ({
      projections: { ...state.projections, [sessionId]: projection },
    }));
    return projection.needsSnapshot;
  },
  applyBackgroundJobReceipt(sessionId, receipt) {
    const current = get().projections[sessionId] ?? createAgentProjection(sessionId);
    const projection = applyAgentBackgroundJobReceipt(current, receipt);
    if (projection === current) return false;
    set((state) => ({
      projections: { ...state.projections, [sessionId]: projection },
    }));
    return true;
  },
  appendOptimistic(sessionId, input) {
    const current = get().projections[sessionId] ?? createAgentProjection(sessionId);
    const projection = appendOptimisticAgentMessage(current, input);
    set((state) => ({
      projections: { ...state.projections, [sessionId]: projection },
    }));
  },
  rewriteOptimistic(sessionId, targetMessageId, input) {
    const current = get().projections[sessionId] ?? createAgentProjection(sessionId);
    const projection = rewriteOptimisticAgentMessage(current, targetMessageId, input);
    set((state) => ({
      projections: { ...state.projections, [sessionId]: projection },
    }));
  },
  discardOptimistic(sessionId, clientMessageId) {
    const current = get().projections[sessionId];
    if (!current) return;
    const projection = discardOptimisticAgentMessage(current, clientMessageId);
    set((state) => ({
      projections: { ...state.projections, [sessionId]: projection },
    }));
  },
  failOptimistic(
    sessionId,
    clientMessageId,
    error,
    nowMs,
    admissionState,
  ) {
    const current = get().projections[sessionId];
    if (!current) return;
    const projection = failOptimisticAgentMessage(
      current,
      clientMessageId,
      error,
      nowMs,
      admissionState,
    );
    set((state) => ({
      projections: { ...state.projections, [sessionId]: projection },
    }));
  },
  requeueOptimistic(sessionId, clientMessageId, nowMs) {
    const current = get().projections[sessionId];
    if (!current) return;
    const projection = requeueOptimisticAgentMessage(
      current,
      clientMessageId,
      nowMs,
    );
    set((state) => ({
      projections: { ...state.projections, [sessionId]: projection },
    }));
  },
  acknowledgeOptimistic(sessionId, clientMessageId, nowMs) {
    const current = get().projections[sessionId];
    if (!current) return;
    const projection = acknowledgeOptimisticAgentMessage(
      current,
      clientMessageId,
      nowMs,
    );
    if (projection === current) return;
    set((state) => ({
      projections: { ...state.projections, [sessionId]: projection },
    }));
  },
  abortTurn(sessionId, turnId, nowMs) {
    const current = get().projections[sessionId];
    if (!current) return;
    const projection = abortAgentTurn(current, turnId, nowMs);
    set((state) => ({
      projections: { ...state.projections, [sessionId]: projection },
    }));
  },
  clear(sessionId) {
    set((state) => {
      const projections = { ...state.projections };
      delete projections[sessionId];
      return { projections };
    });
  },
}));

export function agentProjection(sessionId: string): AgentLiveProjection {
  return (
    useAgentLiveStore.getState().projections[sessionId] ?? createAgentProjection(sessionId)
  );
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
  if (!['idle', 'ready', 'stopped', 'active', 'failed', 'faulted'].includes(projection.status)) {
    return false;
  }
  return !projection.turnOrder.some((turnId) => (
    ['queued', 'running', 'waiting'].includes(projection.turnsById[turnId]?.status ?? '')
  ));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
