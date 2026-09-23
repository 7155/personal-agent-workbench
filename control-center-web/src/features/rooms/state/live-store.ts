import { create, type StoreApi } from 'zustand';

import {
  abortRoomParticipantTurn,
  abortRoomTurn,
  appendOptimisticRoomMessage,
  createRoomProjection,
  reduceRoomEvents,
  replayRoomConversationSnapshot,
  replayRoomEventSnapshot,
  type RoomConversationSnapshot,
  type RoomEventPage,
  type RoomEventSnapshot,
  type OptimisticRoomMessageInput,
  type RoomProjectionState,
} from '@/contracts/room-reducer';
import type { UiRoomEvent } from '@/contracts/ui-events';
import { mergeAcceptedRoomTimeline } from '../runtime/accepted-room-timeline';
import { publishRoomProjectionSnapshot } from './projection-bridge';
import { acceptedRoomEvents, appendRoomEventWindow, canApplyRoomSnapshot } from './room-event-window';

export interface RoomHistoryWindow {
  events: readonly UiRoomEvent[];
  firstSequence: number;
  hasMore: boolean;
  retainedPrefixTruncated: boolean;
}

interface RoomLiveStore {
  projections: Record<string, RoomProjectionState>;
  roomRevisions: Record<string, number>;
  turnRevisions: Record<string, Record<string, number>>;
  historyByRoomId: Record<string, RoomHistoryWindow>;
  snapshotsByRoomId: Record<string, RoomEventSnapshot>;
  ensure(roomId: string): void;
  replaySnapshot(roomId: string, snapshot: RoomEventSnapshot): boolean;
  replaySnapshotWithTail(
    roomId: string,
    snapshot: RoomEventSnapshot,
    liveTail: readonly UiRoomEvent[],
  ): boolean;
  replayConversationSnapshot(roomId: string, snapshot: RoomConversationSnapshot): boolean;
  applyEvents(roomId: string, events: readonly UiRoomEvent[]): boolean;
  prependHistory(roomId: string, page: RoomEventPage): boolean;
  appendOptimistic(
    roomId: string,
    input: OptimisticRoomMessageInput,
  ): void;
  acceptMessage(roomId: string, response: Record<string, unknown>): void;
  discardOptimistic(roomId: string, clientMessageId: string): void;
  abortTurn(roomId: string, turnId: string, nowMs: number): void;
  abortParticipant(
    roomId: string,
    turnId: string,
    participantId: string,
    nowMs: number,
  ): void;
  remove(roomId: string): void;
  reset(): void;
}

export const useRoomLiveStore = create<RoomLiveStore>((set, get) => ({
  projections: {},
  roomRevisions: {},
  turnRevisions: {},
  historyByRoomId: {},
  snapshotsByRoomId: {},
  ensure(roomId) {
    if (!roomId || get().projections[roomId]) return;
    set((state) => ({
      projections: {
        ...state.projections,
        [roomId]: createRoomProjection(roomId),
      },
    }));
  },
  replaySnapshot(roomId, snapshot) {
    const current = roomProjection(roomId);
    if (!canApplyRoomSnapshot(current, snapshot.lastSequence)) return false;
    const rewound = snapshot.lastSequence < current.lastSequence;
    const merged = mergeSnapshotWindow(snapshot, rewound ? undefined : get().historyByRoomId[roomId]);
    const next = replayRoomEventSnapshot(current, merged.snapshot);
    set((state) => ({
      historyByRoomId: {
        ...state.historyByRoomId,
        [roomId]: merged.window,
      },
      snapshotsByRoomId: {
        ...state.snapshotsByRoomId,
        [roomId]: merged.snapshot,
      },
    }));
    replaceProjection(set, get, roomId, next, allTurnIds(current, next));
    return true;
  },
  replaySnapshotWithTail(roomId, snapshot, liveTail) {
    const current = roomProjection(roomId);
    // A deferred enrichment is not the recovery owner. Wait for the fresh
    // authoritative snapshot rather than mixing pre-gap or pre-restore tails.
    if (current.needsSnapshot) return false;
    const tail = appendRoomEventWindow(roomId, snapshot.events, liveTail);
    const latestTailSequence = tail.at(-1)?.sequence ?? snapshot.lastSequence;
    if (latestTailSequence < current.lastSequence) return false;
    const merged = mergeSnapshotWindow(snapshot, get().historyByRoomId[roomId]);
    const events = appendRoomEventWindow(roomId, merged.window.events, liveTail);
    const latest = events.at(-1);
    const replaySnapshot: RoomEventSnapshot = {
      ...merged.snapshot,
      room: {
        ...merged.snapshot.room,
        lastEventSequence: latest?.sequence ?? merged.snapshot.lastSequence,
      },
      events,
      firstSequence: events[0]?.sequence ?? 0,
      lastSequence: latest?.sequence ?? merged.snapshot.lastSequence,
      resumeToken: latest?.resumeToken ?? merged.snapshot.resumeToken,
      truncated: (events[0]?.sequence ?? 0) > 1,
    };
    const next = replayRoomEventSnapshot(current, replaySnapshot);
    set((state) => ({
      historyByRoomId: {
        ...state.historyByRoomId,
        [roomId]: {
          ...merged.window,
          events,
          firstSequence: replaySnapshot.firstSequence,
        },
      },
      snapshotsByRoomId: {
        ...state.snapshotsByRoomId,
        [roomId]: replaySnapshot,
      },
    }));
    replaceProjection(set, get, roomId, next, allTurnIds(current, next));
    return true;
  },
  replayConversationSnapshot(roomId, snapshot) {
    const current = roomProjection(roomId);
    // Warm projections render immediately and keep their richer Tool history.
    // Their existing cursor can catch up through SSE; the lightweight snapshot
    // is the cold-open path, not permission to erase already loaded evidence.
    if (current.lastSequence > 0) return false;
    const next = replayRoomConversationSnapshot(current, snapshot);
    replaceProjection(set, get, roomId, next, allTurnIds(current, next));
    return true;
  },
  applyEvents(roomId, events) {
    const current = roomProjection(roomId);
    const projection = reduceRoomEvents(current, events);
    const accepted = acceptedRoomEvents(current, projection, events);
    const changedTurnIds = new Set<string>();
    for (const event of events) {
      if (event.turnId) changedTurnIds.add(event.turnId);
    }
    const baseSnapshot = get().snapshotsByRoomId[roomId];
    const currentWindow = get().historyByRoomId[roomId];
    if (baseSnapshot && currentWindow && accepted.length) {
      const mergedEvents = appendRoomEventWindow(roomId, currentWindow.events, accepted);
      const firstSequence = mergedEvents[0]?.sequence ?? 0;
      const latest = mergedEvents.at(-1);
      const updatedSnapshot: RoomEventSnapshot = {
        ...baseSnapshot,
        room: {
          ...baseSnapshot.room,
          lastEventSequence: latest?.sequence ?? baseSnapshot.room.lastEventSequence,
        },
        events: mergedEvents,
        firstSequence,
        lastSequence: latest?.sequence ?? baseSnapshot.lastSequence,
        resumeToken: latest?.resumeToken ?? baseSnapshot.resumeToken,
        truncated: firstSequence > 1,
      };
      set((state) => ({
        historyByRoomId: {
          ...state.historyByRoomId,
          [roomId]: {
            ...currentWindow,
            events: mergedEvents,
            firstSequence,
            hasMore: currentWindow.hasMore || firstSequence > currentWindow.firstSequence,
          },
        },
        snapshotsByRoomId: {
          ...state.snapshotsByRoomId,
          [roomId]: updatedSnapshot,
        },
      }));
    }
    if (projection !== current) {
      replaceProjection(set, get, roomId, projection, changedTurnIds);
    }
    return projection.needsSnapshot;
  },
  prependHistory(roomId, page) {
    if (page.roomId !== roomId) return false;
    const current = roomProjection(roomId);
    const window = get().historyByRoomId[roomId];
    const snapshot = get().snapshotsByRoomId[roomId];
    if (!window || !snapshot) return false;
    if (!page.items.length) {
      set((state) => ({
        historyByRoomId: {
          ...state.historyByRoomId,
          [roomId]: {
            ...window,
            hasMore: false,
            retainedPrefixTruncated: page.retainedPrefixTruncated,
          },
        },
      }));
      return true;
    }
    if (
      !window.firstSequence
      || page.lastSequence !== window.firstSequence - 1
    ) return false;
    // The server bounds the initial live snapshot. Pages explicitly requested
    // by the reader are durable UI history and must not be discarded merely
    // because they extend beyond that live snapshot window.
    const events = [...page.items, ...window.events];
    const mergedSnapshot: RoomEventSnapshot = {
      ...snapshot,
      events,
      firstSequence: events[0]?.sequence ?? 0,
      truncated: (events[0]?.sequence ?? 0) > 1,
    };
    const next = replayRoomEventSnapshot(current, mergedSnapshot);
    set((state) => ({
      historyByRoomId: {
        ...state.historyByRoomId,
        [roomId]: {
          events,
          firstSequence: mergedSnapshot.firstSequence,
          hasMore: page.hasMore,
          retainedPrefixTruncated: page.retainedPrefixTruncated,
        },
      },
      snapshotsByRoomId: {
        ...state.snapshotsByRoomId,
        [roomId]: mergedSnapshot,
      },
    }));
    replaceProjection(set, get, roomId, next, allTurnIds(current, next));
    return true;
  },
  appendOptimistic(roomId, input) {
    const current = roomProjection(roomId);
    const next = appendOptimisticRoomMessage(current, input);
    const messageId = next.optimisticByClientMessageId[input.clientMessageId];
    const turnId = messageId ? next.messagesById[messageId]?.turnId : '';
    replaceProjection(set, get, roomId, next, turnId ? [turnId] : []);
  },
  acceptMessage(roomId, response) {
    const current = roomProjection(roomId);
    const next = mergeAcceptedRoomTimeline(current, response);
    replaceProjection(set, get, roomId, next, allTurnIds(current, next));
  },
  discardOptimistic(roomId, clientMessageId) {
    const current = roomProjection(roomId);
    const messageId = current.optimisticByClientMessageId[clientMessageId];
    const turnId = messageId ? current.messagesById[messageId]?.turnId : '';
    const next = discardOptimisticRoomMessage(current, clientMessageId);
    replaceProjection(set, get, roomId, next, turnId ? [turnId] : []);
  },
  abortTurn(roomId, turnId, nowMs) {
    const current = roomProjection(roomId);
    const next = abortRoomTurn(current, turnId, nowMs);
    replaceProjection(set, get, roomId, next, [turnId]);
  },
  abortParticipant(roomId, turnId, participantId, nowMs) {
    const current = roomProjection(roomId);
    const next = abortRoomParticipantTurn(
      current,
      turnId,
      participantId,
      nowMs,
    );
    replaceProjection(set, get, roomId, next, [turnId]);
  },
  remove(roomId) {
    set((state) => {
      const projections = { ...state.projections };
      const roomRevisions = { ...state.roomRevisions };
      const turnRevisions = { ...state.turnRevisions };
      const historyByRoomId = { ...state.historyByRoomId };
      const snapshotsByRoomId = { ...state.snapshotsByRoomId };
      delete projections[roomId];
      delete roomRevisions[roomId];
      delete turnRevisions[roomId];
      delete historyByRoomId[roomId];
      delete snapshotsByRoomId[roomId];
      return {
        projections,
        roomRevisions,
        turnRevisions,
        historyByRoomId,
        snapshotsByRoomId,
      };
    });
  },
  reset() {
    set({
      projections: {},
      roomRevisions: {},
      turnRevisions: {},
      historyByRoomId: {},
      snapshotsByRoomId: {},
    });
  },
}));

/* Publish only when the immutable projections record changes. Direct test or
 * recovery setState calls are covered as well as the normal reducer actions,
 * while history/revision-only updates do not wake the shell. */
useRoomLiveStore.subscribe((state, previous) => {
  if (state.projections !== previous.projections) {
    publishRoomProjectionSnapshot(state.projections);
  }
});

function mergeSnapshotWindow(
  snapshot: RoomEventSnapshot,
  existing?: RoomHistoryWindow,
): { snapshot: RoomEventSnapshot; window: RoomHistoryWindow } {
  let events = [...snapshot.events];
  let hasMore = snapshot.firstSequence > 1;
  let retainedPrefixTruncated = false;
  if (existing?.events.length && events.length) {
    const older = existing.events.filter((event) => event.sequence < snapshot.firstSequence);
    if (older.at(-1)?.sequence === snapshot.firstSequence - 1) {
      events = [...older, ...events];
      hasMore = existing.hasMore;
      retainedPrefixTruncated = existing.retainedPrefixTruncated;
    }
  }
  const firstSequence = events[0]?.sequence ?? 0;
  const mergedSnapshot: RoomEventSnapshot = {
    ...snapshot,
    events,
    firstSequence,
    truncated: firstSequence > 1,
  };
  return {
    snapshot: mergedSnapshot,
    window: {
      events,
      firstSequence,
      hasMore,
      retainedPrefixTruncated,
    },
  };
}

export function roomProjection(roomId: string): RoomProjectionState {
  return (
    useRoomLiveStore.getState().projections[roomId]
    ?? createRoomProjection(roomId)
  );
}

export function discardOptimisticRoomMessage(
  state: RoomProjectionState,
  clientMessageId: string,
): RoomProjectionState {
  const messageId = state.optimisticByClientMessageId[clientMessageId];
  if (!messageId) return state;
  const message = state.messagesById[messageId];
  const next: RoomProjectionState = {
    ...state,
    messagesById: { ...state.messagesById },
    messageOrder: state.messageOrder.filter((id) => id !== messageId),
    turnsById: { ...state.turnsById },
    turnOrder: [...state.turnOrder],
    optimisticByClientMessageId: { ...state.optimisticByClientMessageId },
  };
  delete next.messagesById[messageId];
  delete next.optimisticByClientMessageId[clientMessageId];
  if (!message) return next;
  const turn = next.turnsById[message.turnId];
  if (!turn) return next;
  const messageIds = turn.messageIds.filter((id) => id !== messageId);
  if (messageIds.length || turn.activityIds.length) {
    next.turnsById[turn.id] = { ...turn, messageIds };
  } else {
    delete next.turnsById[turn.id];
    next.turnOrder = next.turnOrder.filter((id) => id !== turn.id);
  }
  return next;
}

function replaceProjection(
  set: StoreApi<RoomLiveStore>['setState'],
  get: StoreApi<RoomLiveStore>['getState'],
  roomId: string,
  projection: RoomProjectionState,
  changedTurnIds: Iterable<string>,
): void {
  const current = get().projections[roomId];
  if (current === projection) return;
  set((state) => {
    const roomTurnRevisions = { ...(state.turnRevisions[roomId] ?? {}) };
    for (const turnId of changedTurnIds) {
      if (!turnId) continue;
      roomTurnRevisions[turnId] = (roomTurnRevisions[turnId] ?? 0) + 1;
    }
    return {
      projections: { ...state.projections, [roomId]: projection },
      roomRevisions: {
        ...state.roomRevisions,
        [roomId]: (state.roomRevisions[roomId] ?? 0) + 1,
      },
      turnRevisions: {
        ...state.turnRevisions,
        [roomId]: roomTurnRevisions,
      },
    };
  });
}

function allTurnIds(
  current: RoomProjectionState,
  next: RoomProjectionState,
): Set<string> {
  return new Set([...current.turnOrder, ...next.turnOrder]);
}
