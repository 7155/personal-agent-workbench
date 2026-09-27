import { create } from 'zustand';
import {
  applyAgentBackgroundJobReceipt,
  acknowledgeOptimisticAgentMessage,
  abortAgentTurn,
  agentSnapshotFromResponse,
  appendOptimisticAgentMessage,
  applyAgentSnapshot,
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

interface AgentLiveStore {
  projections: Record<string, AgentProjectionState>;
  ensure(sessionId: string): void;
  hydrate(sessionId: string, value: unknown): boolean;
  hydrateSnapshot(sessionId: string, snapshot: AgentSnapshot): boolean;
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
  hydrate(sessionId, value) {
    if (isRecord(value) && (
      value.ok === false
      || (typeof value.sessionId === 'string' && value.sessionId !== sessionId)
    )) return false;
    return get().hydrateSnapshot(sessionId, agentSnapshotFromResponse(value));
  },
  hydrateSnapshot(sessionId, snapshot) {
    const current = get().projections[sessionId] ?? createAgentProjection(sessionId);
    if (snapshot.lastSequence < current.lastSequence) return false;
    // A snapshot with no messages can only be a transient/partial projection
    // failure for a Session that already has durable history. Rebuild the
    // cursor and terminal/status metadata against the last confirmed
    // messages, then retain prior activity rows that this empty response
    // cannot disprove.
    // This keeps an idle terminal snapshot able to clear a stale spinner
    // without allowing a successful-looking empty response to erase text.
    const preserveHistory = (
      current.messageOrder.length > 0
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
    const projection = applyAgentSnapshot(current, normalizeLegacyHistoryTurns(hydratedSnapshot), {
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
    const projection = reduceAgentEvents(current, events);
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

export function agentProjection(sessionId: string): AgentProjectionState {
  return (
    useAgentLiveStore.getState().projections[sessionId] ?? createAgentProjection(sessionId)
  );
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
