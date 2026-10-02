import { afterEach, describe, expect, it } from 'vitest';
import type { RoomConversationSnapshot, RoomEventPage, RoomEventSnapshot } from '@/contracts/room-reducer';
import { roomEventFixture } from '@/test/fixtures/events';

import { selectActivePublicRoomTurn } from '../runtime/room-execution-lanes';
import { roomProjection, useRoomLiveStore } from './live-store';

describe('Room live store', () => {
  afterEach(() => useRoomLiveStore.getState().reset());

  it('isolates projections by Room and retains in-flight messages across navigation', () => {
    const store = useRoomLiveStore.getState();
    store.appendOptimistic('room:a', {
      clientMessageId: 'client:a',
      text: '继续执行',
      nowMs: 10,
    });

    expect(roomProjection('room:a').messageOrder).toEqual(['local-room:client:a']);
    expect(roomProjection('room:b').messageOrder).toEqual([]);
    expect(useRoomLiveStore.getState().roomRevisions['room:a']).toBe(1);
    expect(useRoomLiveStore.getState().roomRevisions['room:b']).toBeUndefined();
  });

  it('increments only the changed turn revision', () => {
    const store = useRoomLiveStore.getState();
    store.appendOptimistic('room:a', {
      clientMessageId: 'client:first',
      text: '第一条',
      nowMs: 10,
    });
    store.appendOptimistic('room:a', {
      clientMessageId: 'client:second',
      text: '第二条',
      nowMs: 20,
    });

    const [firstTurnId, secondTurnId] = roomProjection('room:a').turnOrder;
    const before = useRoomLiveStore.getState().turnRevisions['room:a'];
    expect(before?.[firstTurnId!]).toBe(1);
    expect(before?.[secondTurnId!]).toBe(1);

    store.discardOptimistic('room:a', 'client:first');

    const after = useRoomLiveStore.getState().turnRevisions['room:a'];
    expect(after?.[firstTurnId!]).toBe(2);
    expect(after?.[secondTurnId!]).toBe(1);
    expect(roomProjection('room:a').turnOrder).toEqual([secondTurnId]);
  });

  it('keeps a newer live projection when reconnect hydration returns an older snapshot', () => {
    const store = useRoomLiveStore.getState();
    store.applyEvents('room-1', [
      roomEventFixture(1, 'participant_status', { status: 'working' }),
    ]);
    const confirmed = roomProjection('room-1');
    const revision = useRoomLiveStore.getState().roomRevisions['room-1'];
    const staleSnapshot = {
      lastSequence: 0,
      room: { id: 'room-1' },
      resumeToken: '',
    } as unknown as RoomEventSnapshot;

    expect(store.replaySnapshot('room-1', staleSnapshot)).toBe(false);
    expect(roomProjection('room-1')).toBe(confirmed);
    expect(roomProjection('room-1').resumeToken).toBe('room-1:1');
    expect(useRoomLiveStore.getState().roomRevisions['room-1']).toBe(revision);
  });

  it('hydrates a cold message-first snapshot without replacing a warm rich projection', () => {
    const store = useRoomLiveStore.getState();
    const initial = roomConversationSnapshot(
      [roomEventFixture(1, 'user_message', { messageId: 'message-1', text: '消息优先' })],
      50,
    );

    expect(store.replayConversationSnapshot('room-1', initial)).toBe(true);
    expect(roomProjection('room-1').lastSequence).toBe(50);
    expect(roomProjection('room-1').messagesById['message-1']?.text).toBe('消息优先');

    const warm = roomProjection('room-1');
    expect(store.replayConversationSnapshot(
      'room-1',
      roomConversationSnapshot([], 60),
    )).toBe(false);
    expect(roomProjection('room-1')).toBe(warm);
  });

  it('prepends bounded history pages and preserves them across a newer snapshot', () => {
    const store = useRoomLiveStore.getState();
    const recent = [
      roomEventFixture(3, 'participant_status', { status: 'working' }),
      roomEventFixture(4, 'participant_status', { status: 'completed' }),
    ];
    expect(store.replaySnapshot(
      'room-1',
      roomHistorySnapshot(recent, 3, 4),
    )).toBe(true);
    expect(useRoomLiveStore.getState().historyByRoomId['room-1']?.hasMore).toBe(true);

    const older = [
      roomEventFixture(1, 'participant_status', { status: 'assigned' }),
      roomEventFixture(2, 'participant_status', { status: 'accepted' }),
    ];
    const page = {
      schemaVersion: 'rag-ime.agent-room-event-page.v1',
      ok: true,
      roomId: 'room-1',
      items: older,
      firstSequence: 1,
      lastSequence: 2,
      nextBeforeSequence: 0,
      hasMore: false,
      retainedFirstSequence: 1,
      retainedLastSequence: 4,
      retainedPrefixTruncated: false,
    } satisfies RoomEventPage;

    expect(store.prependHistory('room-1', page)).toBe(true);
    expect(
      useRoomLiveStore.getState().historyByRoomId['room-1']?.events.map((event) => event.sequence),
    ).toEqual([1, 2, 3, 4]);

    expect(store.replaySnapshot(
      'room-1',
      roomHistorySnapshot([
        roomEventFixture(4, 'participant_status', { status: 'completed' }),
        roomEventFixture(5, 'turn_completed', {}),
      ], 4, 5),
    )).toBe(true);
    expect(
      useRoomLiveStore.getState().historyByRoomId['room-1']?.events.map((event) => event.sequence),
    ).toEqual([1, 2, 3, 4, 5]);
    expect(useRoomLiveStore.getState().historyByRoomId['room-1']?.hasMore).toBe(false);
  });

  it('keeps a terminal latest root authoritative after reconnect and older history hydration', () => {
    const store = useRoomLiveStore.getState();
    const latestRootId = 'room-1:latest-root';
    const latestDispatchId = 'room-1:latest-dispatch';
    const recent = [
      { ...roomEventFixture(3, 'user_message', { text: '最新任务' }), turnId: latestRootId },
      { ...roomEventFixture(4, 'turn_completed', {
        rootId: latestRootId,
        dispatchId: latestDispatchId,
        status: 'completed',
      }),
        turnId: latestRootId,
        participantId: 'participant-1',
        sourceSessionId: 'session-1',
      },
    ];

    expect(store.replaySnapshot('room-1', roomHistorySnapshot(recent, 3, 4))).toBe(true);
    expect(selectActivePublicRoomTurn(roomProjection('room-1'))).toBeUndefined();

    const staleRootId = 'room-1:stale-root';
    const older = [
      { ...roomEventFixture(1, 'user_message', { text: '旧任务' }), turnId: staleRootId },
      { ...roomEventFixture(2, 'participant_status', { status: 'working' }),
        turnId: staleRootId,
        participantId: 'participant-1',
        sourceSessionId: 'session-1',
      },
    ];
    const page = {
      schemaVersion: 'rag-ime.agent-room-event-page.v1',
      ok: true,
      roomId: 'room-1',
      items: older,
      firstSequence: 1,
      lastSequence: 2,
      nextBeforeSequence: 0,
      hasMore: false,
      retainedFirstSequence: 1,
      retainedLastSequence: 4,
      retainedPrefixTruncated: false,
    } satisfies RoomEventPage;

    expect(store.prependHistory('room-1', page)).toBe(true);
    expect(roomProjection('room-1').turnsById[staleRootId]?.status).toBe('running');
    expect(roomProjection('room-1').turnsById[latestRootId]?.status).toBe('completed');
    expect(selectActivePublicRoomTurn(roomProjection('room-1'))).toBeUndefined();
  });

  it('keeps explicitly loaded history beyond the live retention window', () => {
    const store = useRoomLiveStore.getState();
    const recent = Array.from({ length: 2_000 }, (_value, index) => (
      roomEventFixture(index + 201, 'participant_status', { status: 'working' })
    ));
    expect(store.replaySnapshot(
      'room-1',
      roomHistorySnapshot(recent, 201, 2_200),
    )).toBe(true);

    const older = Array.from({ length: 200 }, (_value, index) => (
      roomEventFixture(index + 1, 'participant_status', { status: 'completed' })
    ));
    const page = {
      schemaVersion: 'rag-ime.agent-room-event-page.v1',
      ok: true,
      roomId: 'room-1',
      items: older,
      firstSequence: 1,
      lastSequence: 200,
      nextBeforeSequence: 0,
      hasMore: false,
      retainedFirstSequence: 1,
      retainedLastSequence: 2_200,
      retainedPrefixTruncated: false,
    } satisfies RoomEventPage;

    expect(store.prependHistory('room-1', page)).toBe(true);
    const history = useRoomLiveStore.getState().historyByRoomId['room-1'];
    expect(history?.events).toHaveLength(2_200);
    expect(history?.events[0]?.sequence).toBe(1);
    expect(history?.events.at(-1)?.sequence).toBe(2_200);
    expect(history?.hasMore).toBe(false);
  });

  it('publishes HTTP acknowledgements and their cached history atomically before duplicate SSE replay', () => {
    const store = useRoomLiveStore.getState();
    const initial = roomEventFixture(3, 'participant_status', { status: 'working' });
    store.replaySnapshot('room-1', roomHistorySnapshot([initial], 3, 3));
    store.appendOptimistic('room-1', { clientMessageId: 'client-4', text: '新消息', nowMs: 35 });
    const acknowledged = roomEventFixture(4, 'user_message', {
      messageId: 'message-4', clientMessageId: 'client-4', text: '新消息',
    });
    const seen: number[][] = [];
    const unsubscribe = useRoomLiveStore.subscribe((state) => {
      seen.push([
        state.projections['room-1']!.lastSequence,
        state.historyByRoomId['room-1']!.events.at(-1)!.sequence,
        state.snapshotsByRoomId['room-1']!.lastSequence,
      ]);
    });
    store.acceptMessage('room-1', acknowledgement([acknowledged]));
    unsubscribe();
    expect(seen).toEqual([[4, 4, 4]]);
    expect(roomProjection('room-1').messageOrder).toEqual(['message-4']);
    const afterAck = useRoomLiveStore.getState();
    store.acceptMessage('room-1', acknowledgement([acknowledged]));
    expect(useRoomLiveStore.getState()).toBe(afterAck);
    store.applyEvents('room-1', [acknowledged, roomEventFixture(5, 'participant_status', { status: 'working' })]);
    expect(useRoomLiveStore.getState().historyByRoomId['room-1']!.events.map(event => event.sequence)).toEqual([3, 4, 5]);
    expect(store.prependHistory('room-1', {
      schemaVersion: 'rag-ime.agent-room-event-page.v1', ok: true, roomId: 'room-1',
      items: [1, 2].map(sequence => roomEventFixture(sequence, 'participant_status', { status: 'working' })),
      firstSequence: 1, lastSequence: 2, nextBeforeSequence: 0, hasMore: false,
      retainedFirstSequence: 1, retainedLastSequence: 5, retainedPrefixTruncated: false,
    })).toBe(true);
    expect(roomProjection('room-1').messagesById['message-4']?.text).toBe('新消息');
    expect(roomProjection('room-1').lastSequence).toBe(5);
  });

  it('retains acknowledged events when a reconnect snapshot reuses the cached prefix', () => {
    const store = useRoomLiveStore.getState();
    const events = Array.from({ length: 10 }, (_, index) => roomEventFixture(index + 1,
      index === 2 ? 'user_message' : 'participant_status',
      index === 2 ? { messageId: 'ack-message', text: '保留回执' } : { status: 'working' }));
    store.replaySnapshot('room-1', roomHistorySnapshot(events.slice(0, 2), 1, 2));
    store.acceptMessage('room-1', acknowledgement([events[2]!]));
    store.applyEvents('room-1', events.slice(2));
    expect(store.replaySnapshot('room-1', roomHistorySnapshot(events.slice(4), 5, 10))).toBe(true);
    expect(useRoomLiveStore.getState().historyByRoomId['room-1']!.events.map(event => event.sequence)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
    expect(roomProjection('room-1').messagesById['ack-message']?.text).toBe('保留回执');
  });

  it('ignores an HTTP acknowledgement after SSE has already published the same event', () => {
    const store = useRoomLiveStore.getState();
    store.replaySnapshot('room-1', roomHistorySnapshot([roomEventFixture(1, 'participant_status', { status: 'working' })], 1, 1));
    const message = roomEventFixture(2, 'user_message', { messageId: 'sse-first', text: '先收到事件' });
    store.applyEvents('room-1', [message]);
    const before = useRoomLiveStore.getState();
    store.acceptMessage('room-1', acknowledgement([message]));
    expect(useRoomLiveStore.getState()).toBe(before);
    expect(before.historyByRoomId['room-1']!.events.map(event => event.sequence)).toEqual([1, 2]);
  });

  it('rejects the whole HTTP acknowledgement on a gap without changing any projection or cache', () => {
    const store = useRoomLiveStore.getState();
    store.replaySnapshot('room-1', roomHistorySnapshot([roomEventFixture(1, 'participant_status', { status: 'working' })], 1, 1));
    const before = useRoomLiveStore.getState();
    store.acceptMessage('room-1', acknowledgement([
      roomEventFixture(2, 'user_message', { messageId: 'partial-message', text: '尚未确认' }),
      roomEventFixture(4, 'participant_status', { status: 'working' }),
    ]));
    expect(useRoomLiveStore.getState()).toBe(before);
    expect(roomProjection('room-1').needsSnapshot).toBe(false);
  });

  it.each(['completed', 'failed'] as const)('preserves an observed %s Root when a delayed cancellation receipt arrives', (status) => {
    const store = useRoomLiveStore.getState();
    store.applyEvents('room-1', [
      roomEventFixture(1, 'user_message', { text: '任务' }),
      { ...roomEventFixture(2, status === 'failed' ? 'turn_failed' : 'turn_completed', { status }), participantId: null, sourceSessionId: '' },
    ]);
    const before = useRoomLiveStore.getState();
    store.abortTurn('room-1', 'room-turn-1', 100);
    expect(useRoomLiveStore.getState()).toBe(before);
    expect(roomProjection('room-1').turnsById['room-turn-1']).toMatchObject({ status, rootTerminalAtMs: 20 });
  });

  it.each([
    ['failed', 'completed'], ['failed', 'failed'], ['failed', 'aborted'],
    ['aborted', 'completed'], ['aborted', 'failed'], ['aborted', 'aborted'],
  ] as const)('retains a %s message receipt when its Root later becomes %s', (messageStatus, rootStatus) => {
    const store = useRoomLiveStore.getState();
    store.applyEvents('room-1', [
      roomEventFixture(1, 'user_message', { text: '任务', mode: 'jev', graphId: 'graph-1' }),
      roomEventFixture(2, 'route_decision', { dispatchId: 'dispatch-1', targetParticipantId: 'participant-1' }),
      roomEventFixture(3, 'participant_delta', { dispatchId: 'dispatch-1', messageId: 'terminal-message', delta: '已执行部分' }),
      roomEventFixture(4, messageStatus === 'failed' ? 'turn_failed' : 'turn_completed', { dispatchId: 'dispatch-1', status: messageStatus }),
      { ...roomEventFixture(5, rootStatus === 'failed' ? 'turn_failed' : 'turn_completed', { status: rootStatus }), participantId: null, sourceSessionId: '' },
    ]);
    const message = Object.values(roomProjection('room-1').messagesById).find(item => item.dispatchId === 'dispatch-1');
    expect(message).toMatchObject({ status: messageStatus, completedAtMs: 40 });
    expect(roomProjection('room-1').turnsById['room-turn-1']).toMatchObject({ status: rootStatus, rootTerminalAtMs: 50 });
  });

  it('drops every projection only when an explicit reset is requested', () => {
    const store = useRoomLiveStore.getState();
    store.ensure('room:a');
    store.ensure('room:b');
    store.reset();

    expect(useRoomLiveStore.getState().projections).toEqual({});
    expect(useRoomLiveStore.getState().roomRevisions).toEqual({});
    expect(useRoomLiveStore.getState().turnRevisions).toEqual({});
  });
});

function roomHistorySnapshot(
  events: RoomEventSnapshot['events'],
  firstSequence: number,
  lastSequence: number,
): RoomEventSnapshot {
  return {
    schemaVersion: 'rag-ime.agent-room-snapshot.v1',
    ok: true,
    room: {
      id: 'room-1',
      lastEventSequence: lastSequence,
    },
    events,
    firstSequence,
    lastSequence,
    resumeToken: `room-1:${lastSequence}`,
    truncated: firstSequence > 1,
  } as unknown as RoomEventSnapshot;
}

function roomConversationSnapshot(
  events: RoomConversationSnapshot['events'],
  cursorSequence: number,
): RoomConversationSnapshot {
  return {
    schemaVersion: 'rag-ime.agent-room-conversation-snapshot.v1',
    ok: true,
    room: {
      id: 'room-1',
      lastEventSequence: cursorSequence,
    },
    events,
    firstEventSequence: events[0]?.sequence ?? 0,
    cursorSequence,
    resumeToken: cursorSequence ? `room-1:${cursorSequence}` : '',
    deferredEventCount: Math.max(0, cursorSequence - events.length),
    truncated: false,
  } as unknown as RoomConversationSnapshot;
}

function acknowledgement(events: RoomEventSnapshot['events']) {
  return { timelineEvents: events.map(({ streamKind: _streamKind, ...event }) => event) };
}
