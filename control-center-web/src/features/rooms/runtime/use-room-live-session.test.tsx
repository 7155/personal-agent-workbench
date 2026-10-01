import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { roomEventFixture } from '@/test/fixtures/events';
import { MockControlTransport } from '@/test/mock-transport';
import type { ControlEventObserver, ControlSubscription, ControlRequest } from '@/platform/transport';
import { HttpControlTransport } from '@/platform/http-transport';
import type { UiRoomEvent } from '@/contracts/ui-events';
import { preloadRecentConversations } from '@/features/conversation-ui/conversation-preload';
import { roomProjection, useRoomLiveStore } from '../state/live-store';
import { useRoomLiveSession } from './use-room-live-session';

const ROOM_DEFERRED_TEST_DELAY_MS = 120;

afterEach(() => {
  cleanup();
  useRoomLiveStore.getState().reset();
  vi.useRealTimers();
});

describe('useRoomLiveSession snapshot recovery', () => {
  it('opens from a warmed conversation projection without duplicating the light read or keeping an offscreen stream', async () => {
    const conversation = roomConversationSnapshot([], 0, 0);
    const transport = new MockControlTransport({ routes: {
      'agent.room.conversationSnapshot': conversation,
      'agent.room.snapshot': roomSnapshot([]),
    } });
    const warmup = preloadRecentConversations(transport, [{ kind: 'room', id: 'room-1' }]);
    await expect(warmup.promise).resolves.toEqual([
      expect.objectContaining({ kind: 'room', id: 'room-1', status: 'ready' }),
    ]);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.room.conversationSnapshot')).toHaveLength(1);
    expect(transport.subscriptionCalls).toHaveLength(0);

    const onSnapshot = vi.fn();
    const window = renderHook(() => useRoomLiveSession({
      roomId: 'room-1',
      transport,
      onLoadingChange: vi.fn(),
      onSnapshot,
      onMetadata: vi.fn(),
      onConnectionRestored: vi.fn(),
      onConnectionError: vi.fn(),
      onRecoveryState: vi.fn(),
      onEvents: vi.fn(),
    }));
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.room.conversationSnapshot')).toHaveLength(1);
    expect(onSnapshot).toHaveBeenCalledWith('room-1', expect.objectContaining({ schemaVersion: 'rag-ime.agent-room-conversation-snapshot.v1' }));
    window.unmount();
  });

  it('automatically fills a long Room prefix before replacing its visible Jev conversation', async () => {
    vi.useFakeTimers();
    const events = [roomEventFixture(1, 'user_message', {
      messageId: 'original-request', clientMessageId: 'original-client', text: '完整复杂任务', mode: 'jev', graphId: 'graph:long',
    }), ...Array.from({ length: 2200 }, (_, i) => roomEventFixture(i + 2, 'participant_activity', {
      activityKind: 'tool', status: 'completed', toolCallId: `tool-${i}`, summary: `工具 ${i}`,
    }))];
    const tail = roomSnapshot(events.slice(-1000));
    const transport = new MockControlTransport({ routes: {
      'agent.room.conversationSnapshot': roomConversationSnapshot([events[0]!], 2201, 2200),
      'agent.room.snapshot': { ...tail, truncated: true },
      'agent.room.history': (request: ControlRequest) => {
        const before = Number(request.query?.beforeSequence);
        const items = events.filter(event => event.sequence < before).slice(-200);
        const first = items[0]?.sequence ?? 0;
        return { schemaVersion: 'rag-ime.agent-room-event-page.v1', ok: true, roomId: 'room-1',
          items: items.map(({ streamKind: _streamKind, ...event }) => event),
          firstSequence: first, lastSequence: items.at(-1)?.sequence ?? 0,
          nextBeforeSequence: first > 1 ? first : 0, hasMore: first > 1,
          retainedFirstSequence: 1, retainedLastSequence: 2201, retainedPrefixTruncated: false };
      },
    } });
    renderHook(() => useRoomLiveSession({ roomId: 'room-1', transport,
      onLoadingChange: vi.fn(), onSnapshot: vi.fn(), onMetadata: vi.fn(), onConnectionRestored: vi.fn(),
      onConnectionError: vi.fn(), onRecoveryState: vi.fn(), onEvents: vi.fn(),
    }));
    await flushAsyncWork();
    expect(roomProjection('room-1').messagesById['original-request']?.text).toBe('完整复杂任务');
    await act(async () => vi.advanceTimersByTimeAsync(ROOM_DEFERRED_TEST_DELAY_MS));
    await flushAsyncWork();
    expect(useRoomLiveStore.getState().historyByRoomId['room-1']?.events).toHaveLength(2201);
    expect(roomProjection('room-1').messagesById['original-request']?.text).toBe('完整复杂任务');
    expect(roomProjection('room-1').activityOrder).toHaveLength(2200);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.room.history')).toHaveLength(7);
  });

  it('does not initialize an inactive Room and resumes exactly one stream when activated', async () => {
    const transport = new MockControlTransport({
      routes: {
        'agent.room.snapshot': roomSnapshot([]),
      },
    });
    const callbacks = {
      onLoadingChange: vi.fn(),
      onSnapshot: vi.fn(),
      onMetadata: vi.fn(),
      onConnectionRestored: vi.fn(),
      onConnectionError: vi.fn(),
      onRecoveryState: vi.fn(),
      onEvents: vi.fn(),
    };

    const { rerender, unmount } = renderHook(
      ({ active }: { active: boolean }) => useRoomLiveSession({
        active,
        roomId: 'room-1',
        transport,
        ...callbacks,
      }),
      { initialProps: { active: false } },
    );

    await flushAsyncWork();
    expect(transport.requests).toHaveLength(0);
    expect(transport.subscriptionCalls).toHaveLength(0);

    rerender({ active: true });
    await waitFor(() => expect(transport.subscriptionCalls).toHaveLength(1));
    expect(transport.activeSubscriptionCount()).toBe(1);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.room.snapshot')).toHaveLength(1);

    rerender({ active: false });
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(0));
    expect(transport.subscriptionCalls).toHaveLength(1);
    unmount();
  });

  it('shares one authoritative Room stream across simultaneous conversation windows', async () => {
    const transport = new MockControlTransport({
      routes: {
        'agent.room.snapshot': roomSnapshot([]),
      },
    });
    const firstEvents = vi.fn();
    const secondEvents = vi.fn();
    const connectionErrors = vi.fn();
    const callbackDefaults = {
      onLoadingChange: vi.fn(),
      onSnapshot: vi.fn(),
      onMetadata: vi.fn(),
      onConnectionRestored: vi.fn(),
      onConnectionError: connectionErrors,
      onRecoveryState: vi.fn(),
    };

    const { unmount } = renderHook(() => {
      useRoomLiveSession({
        roomId: 'room-1',
        transport,
        ...callbackDefaults,
        onEvents: firstEvents,
      });
      useRoomLiveSession({
        roomId: 'room-1',
        transport,
        ...callbackDefaults,
        onEvents: secondEvents,
      });
    });

    await waitFor(() => expect(transport.subscriptionCalls).toHaveLength(1));
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.room.snapshot')).toHaveLength(1);
    expect(transport.activeSubscriptionCount()).toBe(1);

    const event = roomEventFixture(1, 'participant_activity', {
      activityKind: 'tool',
      status: 'running',
      summary: '正在读取共享记录',
    });
    const rawEvent = Object.fromEntries(
      Object.entries(event).filter(([key]) => key !== 'streamKind'),
    );
    let delivered = 0;
    act(() => {
      delivered = transport.emit('agent.room.events', rawEvent);
    });
    expect(connectionErrors).not.toHaveBeenCalled();
    expect(delivered).toBe(1);
    await flushAsyncWork();
    expect(firstEvents).toHaveBeenCalledWith('room-1', [event]);
    expect(secondEvents).toHaveBeenCalledWith('room-1', [event]);
    unmount();
    expect(transport.activeSubscriptionCount()).toBe(0);
  });

  it('keeps every Room window live when one local listener rejects a terminal update', async () => {
    const transport = new MockControlTransport({
      routes: {
        'agent.room.snapshot': roomSnapshot([]),
      },
    });
    const listenerFailure = new Error('planet window render failed');
    const healthyEvents = vi.fn();
    const connectionErrors = vi.fn();
    const report = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const callbackDefaults = {
      onLoadingChange: vi.fn(),
      onSnapshot: vi.fn(),
      onMetadata: vi.fn(),
      onConnectionRestored: vi.fn(),
      onConnectionError: connectionErrors,
      onRecoveryState: vi.fn(),
    };

    const { unmount } = renderHook(() => {
      useRoomLiveSession({
        roomId: 'room-1',
        transport,
        ...callbackDefaults,
        onEvents: () => { throw listenerFailure; },
      });
      useRoomLiveSession({
        roomId: 'room-1',
        transport,
        ...callbackDefaults,
        onEvents: healthyEvents,
      });
    });

    await waitFor(() => expect(transport.subscriptionCalls).toHaveLength(1));
    const terminal = {
      ...roomEventFixture(1, 'turn_completed', {
        rootId: 'room-turn-1',
        status: 'completed',
      }),
      participantId: null,
      sourceSessionId: '',
    };
    const rawTerminal = Object.fromEntries(
      Object.entries(terminal).filter(([key]) => key !== 'streamKind'),
    );

    act(() => {
      expect(transport.emit('agent.room.events', rawTerminal)).toBe(1);
    });
    await flushAsyncWork();

    expect(healthyEvents).toHaveBeenCalledWith('room-1', [terminal]);
    expect(connectionErrors).not.toHaveBeenCalled();
    expect(transport.activeSubscriptionCount()).toBe(1);
    expect(roomProjection('room-1').lastSequence).toBe(1);
    expect(roomProjection('room-1').turnsById['room-turn-1']?.status).toBe('completed');
    expect(report).toHaveBeenCalledWith(
      'Room live-session listener failed',
      listenerFailure,
    );
    report.mockRestore();
    unmount();
  });


  it('renders conversation events before the deferred Tool snapshot finishes', async () => {
    vi.useFakeTimers();
    const events = [
      roomEventFixture(1, 'user_message', {
        clientMessageId: 'client-message-first',
        messageId: 'message-first',
        text: '先看到消息',
      }),
      roomEventFixture(2, 'participant_activity', {
        activityKind: 'tool',
        status: 'completed',
        summary: '稍后补齐工具回执',
      }),
      roomEventFixture(3, 'turn_completed', { status: 'completed' }),
    ];
    const fullSnapshot = roomSnapshot(events);
    let resolveFullSnapshot: ((value: typeof fullSnapshot) => void) | undefined;
    const deferredFullSnapshot = new Promise<typeof fullSnapshot>((resolve) => {
      resolveFullSnapshot = resolve;
    });
    let fullSnapshotCalls = 0;
    const onLoadingChange = vi.fn();
    const transport = new MockControlTransport({
      routes: {
        'agent.room.conversationSnapshot': roomConversationSnapshot(
          [events[0]!, events[2]!],
          3,
          1,
        ),
        'agent.room.snapshot': () => {
          fullSnapshotCalls += 1;
          return deferredFullSnapshot;
        },
      },
    });

    const { unmount } = renderHook(() => useRoomLiveSession({
      roomId: 'room-1',
      transport,
      onLoadingChange,
      onSnapshot: vi.fn(),
      onMetadata: vi.fn(),
      onConnectionRestored: vi.fn(),
      onConnectionError: vi.fn(),
      onRecoveryState: vi.fn(),
      onEvents: vi.fn(),
    }));

    await flushAsyncWork();
    expect(transport.subscriptionCalls).toHaveLength(1);
    expect(fullSnapshotCalls).toBe(0);
    expect(onLoadingChange).toHaveBeenLastCalledWith(false);
    expect(roomProjection('room-1').messagesById['message-first']?.text).toBe('先看到消息');
    expect(roomProjection('room-1').activityOrder).toHaveLength(0);

    await act(async () => vi.advanceTimersByTimeAsync(ROOM_DEFERRED_TEST_DELAY_MS));
    expect(fullSnapshotCalls).toBe(1);
    await act(async () => {
      resolveFullSnapshot?.(fullSnapshot);
      await deferredFullSnapshot;
    });
    await flushAsyncWork();
    expect(roomProjection('room-1').activityOrder).toHaveLength(1);
    expect(roomProjection('room-1').messagesById['message-first']?.text).toBe('先看到消息');
    unmount();
  });

  it('resumes from the confirmed live cursor when a reconnect snapshot is older', async () => {
    const store = useRoomLiveStore.getState();
    store.applyEvents('room-1', [
      roomEventFixture(1, 'participant_status', { status: 'working' }),
      roomEventFixture(2, 'participant_status', { status: 'working' }),
    ]);
    const onSnapshot = vi.fn();
    const onConnectionError = vi.fn();
    const transport = new MockControlTransport({
      routes: {
        'agent.room.snapshot': roomSnapshot([
          roomEventFixture(1, 'participant_status', { status: 'working' }),
        ]),
      },
    });

    const { unmount } = renderHook(() => useRoomLiveSession({
      roomId: 'room-1',
      transport,
      onLoadingChange: vi.fn(),
      onSnapshot,
      onMetadata: vi.fn(),
      onConnectionRestored: vi.fn(),
      onConnectionError,
      onRecoveryState: vi.fn(),
      onEvents: vi.fn(),
    }));

    await waitFor(() => {
      const error = onConnectionError.mock.calls[0]?.[1];
      if (error instanceof Error) throw error;
      expect(transport.subscriptionCalls).toHaveLength(1);
    });
    expect(transport.subscriptionCalls[0]?.request.lastEventId).toBe('room-1:2');
    expect(roomProjection('room-1').lastSequence).toBe(2);
    expect(onSnapshot).not.toHaveBeenCalled();
    unmount();
  });

  it('recovers a transient stream error without requiring a click', async () => {
    vi.useFakeTimers();
    const onConnectionRestored = vi.fn();
    const onConnectionError = vi.fn();
    const onRecoveryState = vi.fn();
    let snapshotCalls = 0;
    const transport = new ReconnectableMockControlTransport({
      routes: {
        'agent.room.snapshot': () => {
          snapshotCalls += 1;
          return roomSnapshot([]);
        },
      },
    });

    const { unmount } = renderHook(() => useRoomLiveSession({
      roomId: 'room-1',
      transport,
      onLoadingChange: vi.fn(),
      onSnapshot: vi.fn(),
      onMetadata: vi.fn(),
      onConnectionRestored,
      onConnectionError,
      onRecoveryState,
      onEvents: vi.fn(),
    }));

    await flushAsyncWork();
    expect(onConnectionRestored).toHaveBeenCalledTimes(1);
    expect(onRecoveryState).toHaveBeenLastCalledWith('room-1', 'synced');

    act(() => transport.disconnect(new Error('stream interrupted')));
    expect(onConnectionError).toHaveBeenCalledTimes(1);
    expect(onConnectionRestored).toHaveBeenCalledTimes(1);
    expect(onRecoveryState).toHaveBeenLastCalledWith('room-1', 'recovering');

    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(snapshotCalls).toBe(1);
    await act(async () => vi.advanceTimersByTimeAsync(250));
    await flushAsyncWork();
    expect(snapshotCalls).toBe(2);
    expect(onConnectionRestored).toHaveBeenCalledTimes(2);
    expect(onRecoveryState).toHaveBeenLastCalledWith('room-1', 'synced');
    unmount();
  });

  it('does not declare an HTTP Room stream synced from headers alone', async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith('/events')) {
        return new Response(new ReadableStream<Uint8Array>({
          start(controller) {
            streamController = controller;
          },
        }), { headers: { 'Content-Type': 'text/event-stream' } });
      }
      const snapshot = url.pathname.endsWith('/conversation')
        ? roomConversationSnapshot([], 0, 0)
        : roomSnapshot([]);
      return new Response(JSON.stringify(snapshot), {
        headers: { 'Content-Type': 'application/json' },
      });
    }) as typeof fetch;
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: fetchMock,
    });
    const restored = vi.fn();
    const recoveryState = vi.fn();
    const { unmount } = renderHook(() => useRoomLiveSession({
      roomId: 'room-1',
      transport,
      onLoadingChange: vi.fn(),
      onSnapshot: vi.fn(),
      onMetadata: vi.fn(),
      onConnectionRestored: restored,
      onConnectionError: vi.fn(),
      onRecoveryState: recoveryState,
      onEvents: vi.fn(),
    }));

    await waitFor(() => expect(streamController).toBeDefined());
    expect(restored).not.toHaveBeenCalled();
    expect(recoveryState).toHaveBeenLastCalledWith('room-1', 'recovering');
    streamController?.enqueue(new TextEncoder().encode(': heartbeat\n'));
    await Promise.resolve();
    expect(restored).not.toHaveBeenCalled();
    streamController?.enqueue(new TextEncoder().encode('\n'));
    await waitFor(() => expect(restored).toHaveBeenCalledTimes(1));
    expect(recoveryState).toHaveBeenLastCalledWith('room-1', 'synced');
    unmount();
  });

  it('honors a transport Retry-After hint before reloading one Room owner', async () => {
    vi.useFakeTimers();
    let snapshotCalls = 0;
    const transport = new ReconnectableMockControlTransport({
      routes: {
        'agent.room.snapshot': () => {
          snapshotCalls += 1;
          return roomSnapshot([]);
        },
      },
    });
    const { unmount } = renderHook(() => useRoomLiveSession({
      roomId: 'room-1',
      transport,
      onLoadingChange: vi.fn(),
      onSnapshot: vi.fn(),
      onMetadata: vi.fn(),
      onConnectionRestored: vi.fn(),
      onConnectionError: vi.fn(),
      onRecoveryState: vi.fn(),
      onEvents: vi.fn(),
    }));
    await flushAsyncWork();

    act(() => transport.disconnect(
      Object.assign(new Error('event stream capacity'), { retryAfterMs: 2_000 }),
    ));
    await act(async () => vi.advanceTimersByTimeAsync(1_999));
    expect(snapshotCalls).toBe(1);
    await act(async () => vi.advanceTimersByTimeAsync(251));
    await flushAsyncWork();
    expect(snapshotCalls).toBe(2);
    expect(transport.activeSubscriptionCount()).toBe(1);
    unmount();
  });

  it('keeps the live Room healthy when an optional metadata refresh times out', async () => {
    const onConnectionError = vi.fn();
    const onEvents = vi.fn();
    const onRecoveryState = vi.fn();
    const transport = new MockControlTransport({
      routes: {
        'agent.room.snapshot': roomSnapshot([]),
        'agent.room.get': () => { throw new Error('request timed out'); },
      },
    });

    const { unmount } = renderHook(() => useRoomLiveSession({
      roomId: 'room-1',
      transport,
      onLoadingChange: vi.fn(),
      onSnapshot: vi.fn(),
      onMetadata: vi.fn(),
      onConnectionRestored: vi.fn(),
      onConnectionError,
      onRecoveryState,
      onEvents,
    }));

    const metadataEvent = {
      schemaVersion: 'rag-ime.agent-room-event.v1',
      eventId: 'room-1:1',
      roomId: 'room-1',
      sequence: 1,
      turnId: 'room-turn-1',
      eventType: 'participant_activity',
      participantId: 'participant-1',
      sourceSessionId: 'session-room-1',
      createdAtMs: 10,
      payload: { messageId: 'room-message-1', activityKind: 'work', summary: '工作状态已更新' },
      resumeToken: 'room-1:1',
    };
    await waitFor(() => expect(transport.subscriptionCalls).toHaveLength(1));
    expect(transport.subscriptionCalls[0]?.request.pathId).toBe('agent.room.events');
    expect(transport.activeSubscriptionCount()).toBe(1);
    act(() => {
      expect(transport.emit('agent.room.events', metadataEvent)).toBe(1);
    });
    await flushAsyncWork();

    await waitFor(() => expect(
      transport.requests.some(({ request }) => request.pathId === 'agent.room.get'),
    ).toBe(true));
    expect(onConnectionError).not.toHaveBeenCalled();
    expect(onRecoveryState).toHaveBeenLastCalledWith('room-1', 'synced');
    expect(onEvents).toHaveBeenCalled();
    unmount();
  });

  it('automatically retries a failed snapshot after a bounded delay', async () => {
    vi.useFakeTimers();
    let snapshotCalls = 0;
    const onConnectionRestored = vi.fn();
    const onRecoveryState = vi.fn();
    const transport = new MockControlTransport({
      routes: {
        'agent.room.snapshot': () => {
          snapshotCalls += 1;
          if (snapshotCalls === 1) throw new Error('gateway restarting');
          return roomSnapshot([]);
        },
      },
    });

    const { unmount } = renderHook(() => useRoomLiveSession({
      roomId: 'room-1',
      transport,
      onLoadingChange: vi.fn(),
      onSnapshot: vi.fn(),
      onMetadata: vi.fn(),
      onConnectionRestored,
      onConnectionError: vi.fn(),
      onRecoveryState,
      onEvents: vi.fn(),
    }));

    await flushAsyncWork();
    expect(snapshotCalls).toBe(1);
    expect(onRecoveryState).toHaveBeenLastCalledWith('room-1', 'recovering');
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(snapshotCalls).toBe(1);
    await act(async () => vi.advanceTimersByTimeAsync(250));
    await flushAsyncWork();
    expect(snapshotCalls).toBe(2);
    expect(onConnectionRestored).toHaveBeenCalledTimes(1);
    expect(onRecoveryState).toHaveBeenLastCalledWith('room-1', 'synced');
    unmount();
  });

  it('cancels a scheduled recovery when the Room unmounts', async () => {
    vi.useFakeTimers();
    let snapshotCalls = 0;
    const transport = new MockControlTransport({
      routes: {
        'agent.room.snapshot': () => {
          snapshotCalls += 1;
          throw new Error('gateway unavailable');
        },
      },
    });

    const { unmount } = renderHook(() => useRoomLiveSession({
      roomId: 'room-1',
      transport,
      onLoadingChange: vi.fn(),
      onSnapshot: vi.fn(),
      onMetadata: vi.fn(),
      onConnectionRestored: vi.fn(),
      onConnectionError: vi.fn(),
      onRecoveryState: vi.fn(),
      onEvents: vi.fn(),
    }));

    await flushAsyncWork();
    expect(snapshotCalls).toBe(1);
    unmount();
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(snapshotCalls).toBe(1);
  });

  it('resets the recovery delay only after the recovered stream delivers an event', async () => {
    vi.useFakeTimers();
    let snapshotCalls = 0;
    const transport = new ReconnectableMockControlTransport({
      stableOnOpen: false,
      routes: {
        'agent.room.snapshot': () => {
          snapshotCalls += 1;
          return roomSnapshot([]);
        },
      },
    });

    const { unmount } = renderHook(() => useRoomLiveSession({
      roomId: 'room-1',
      transport,
      onLoadingChange: vi.fn(),
      onSnapshot: vi.fn(),
      onMetadata: vi.fn(),
      onConnectionRestored: vi.fn(),
      onConnectionError: vi.fn(),
      onRecoveryState: vi.fn(),
      onEvents: vi.fn(),
    }));

    await flushAsyncWork();
    act(() => transport.disconnect(new Error('first interruption')));
    await act(async () => vi.advanceTimersByTimeAsync(1_250));
    await flushAsyncWork();
    expect(snapshotCalls).toBe(2);

    act(() => transport.disconnect(new Error('second interruption')));
    await act(async () => vi.advanceTimersByTimeAsync(1_250));
    expect(snapshotCalls).toBe(2);
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    await flushAsyncWork();
    expect(snapshotCalls).toBe(3);
    await flushAsyncWork();
    expect(transport.activeSubscriptionCount()).toBe(1);

    const stableEvent = Object.fromEntries(Object.entries(
      roomEventFixture(1, 'participant_status', { status: 'working' }),
    ).filter(([key]) => key !== 'streamKind'));
    act(() => {
      expect(transport.emit('agent.room.events', stableEvent)).toBe(1);
      transport.disconnect(new Error('interruption after stable event'));
    });
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(snapshotCalls).toBe(3);
    await act(async () => vi.advanceTimersByTimeAsync(250));
    await flushAsyncWork();
    expect(snapshotCalls).toBe(4);
    unmount();
  });

  it('coalesces repeated manual retries while a snapshot request is active', async () => {
    let snapshotCalls = 0;
    const emptyConversation = roomConversationSnapshot([], 0, 0);
    let resolveFirstSnapshot: ((value: typeof emptyConversation) => void) | undefined;
    const firstSnapshot = new Promise<typeof emptyConversation>((resolve) => {
      resolveFirstSnapshot = resolve;
    });
    const transport = new MockControlTransport({
      routes: {
        'agent.room.conversationSnapshot': () => {
          snapshotCalls += 1;
          return snapshotCalls === 1 ? firstSnapshot : emptyConversation;
        },
      },
    });

    const { result, unmount } = renderHook(() => useRoomLiveSession({
      roomId: 'room-1',
      transport,
      onLoadingChange: vi.fn(),
      onSnapshot: vi.fn(),
      onMetadata: vi.fn(),
      onConnectionRestored: vi.fn(),
      onConnectionError: vi.fn(),
      onRecoveryState: vi.fn(),
      onEvents: vi.fn(),
    }));

    expect(snapshotCalls).toBe(1);
    act(() => {
      result.current();
      result.current();
      result.current();
    });
    expect(snapshotCalls).toBe(1);
    await act(async () => {
      resolveFirstSnapshot?.(emptyConversation);
      await firstSnapshot;
    });
    await flushAsyncWork();
    expect(snapshotCalls).toBe(2);
    unmount();
  });

  it('offers manual recovery after sustained failures and resets the pending backoff', async () => {
    vi.useFakeTimers();
    let snapshotCalls = 0;
    let gatewayReady = false;
    const onRecoveryState = vi.fn();
    const transport = new MockControlTransport({
      routes: {
        'agent.room.snapshot': () => {
          snapshotCalls += 1;
          if (!gatewayReady) throw new Error('gateway unavailable');
          return roomSnapshot([]);
        },
      },
    });

    const { result, unmount } = renderHook(() => useRoomLiveSession({
      roomId: 'room-1',
      transport,
      onLoadingChange: vi.fn(),
      onSnapshot: vi.fn(),
      onMetadata: vi.fn(),
      onConnectionRestored: vi.fn(),
      onConnectionError: vi.fn(),
      onRecoveryState,
      onEvents: vi.fn(),
    }));

    await flushAsyncWork();
    await act(async () => vi.advanceTimersByTimeAsync(1_250));
    await flushAsyncWork();
    await act(async () => vi.advanceTimersByTimeAsync(2_250));
    await flushAsyncWork();
    expect(snapshotCalls).toBe(3);
    expect(onRecoveryState).toHaveBeenLastCalledWith('room-1', 'failed');

    gatewayReady = true;
    act(() => result.current());
    await flushAsyncWork();
    expect(snapshotCalls).toBe(4);
    expect(onRecoveryState).toHaveBeenLastCalledWith('room-1', 'synced');
    await act(async () => vi.advanceTimersByTimeAsync(15_000));
    expect(snapshotCalls).toBe(4);
    unmount();
  });
});

class ReconnectableMockControlTransport extends MockControlTransport {
  private roomObserver: ControlEventObserver<unknown> | undefined;

  override subscribe<Event = unknown>(
    request: ControlSubscription,
    observer: ControlEventObserver<Event>,
  ): () => void {
    this.roomObserver = observer as ControlEventObserver<unknown>;
    return super.subscribe(request, observer);
  }

  disconnect(error: Error): void {
    this.roomObserver?.error?.(error);
  }

}

async function flushAsyncWork(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}


function roomSnapshot(events: ReturnType<typeof roomEventFixture>[]) {
  const lastSequence = events.at(-1)?.sequence ?? 0;
  return {
    schemaVersion: 'rag-ime.agent-room-snapshot.v1',
    ok: true,
    room: {
      schemaVersion: 'rag-ime.agent-room.v1',
      id: 'room-1',
      title: '协作恢复',
      status: 'active',
      executionMode: 'workspace_managed',
      routingPolicy: 'moderator',
      moderatorParticipantId: 'participant-1',
      permissionPolicy: {
        schemaVersion: 'rag-ime.room-permission-policy.v1',
        room: { executionMode: 'workspace_managed' },
        partner: { executionMode: 'inherit' },
        toolAgent: { executionMode: 'inherit' },
      },
      workspaceRoots: ['/Volumes/work/learnA'],
      createdAtMs: 1,
      updatedAtMs: 2,
      lastEventSequence: lastSequence,
      participants: [{
        schemaVersion: 'rag-ime.agent-participant.v1',
        id: 'participant-1',
        roomId: 'room-1',
        sessionId: 'session-room-1',
        roleId: 'companion-present-v1',
        roleVersion: '1',
        displayName: '澄',
        collaborationRole: 'coordinator',
        status: 'active',
        ordinal: 0,
        createdAtMs: 1,
        lastSpokeAtMs: null,
      },
      {
        schemaVersion: 'rag-ime.agent-participant.v1',
        id: 'participant-2',
        roomId: 'room-1',
        sessionId: 'session-room-2',
        roleId: 'companion-firstlight-v1',
        roleVersion: '1',
        displayName: '澄·初',
        collaborationRole: 'reviewer',
        status: 'active',
        ordinal: 1,
        createdAtMs: 1,
        lastSpokeAtMs: null,
      },
      ],
    },
    events: events.map((event) => Object.fromEntries(
      Object.entries(event).filter(([key]) => key !== 'streamKind'),
    )),
    firstSequence: events[0]?.sequence ?? 0,
    lastSequence,

    resumeToken: lastSequence ? `room-1:${lastSequence}` : '',
    truncated: false,
  };
}
function roomConversationSnapshot(
  events: readonly UiRoomEvent[],
  cursorSequence: number,
  deferredEventCount: number,
) {
  const room = roomSnapshot([]).room;
  return {
    schemaVersion: 'rag-ime.agent-room-conversation-snapshot.v1',
    ok: true,
    room: { ...room, lastEventSequence: cursorSequence },
    events: events.map((event) => Object.fromEntries(
      Object.entries(event).filter(([key]) => key !== 'streamKind'),
    )),
    firstEventSequence: events[0]?.sequence ?? 0,
    cursorSequence,
    resumeToken: cursorSequence ? `room-1:${cursorSequence}` : '',
    deferredEventCount,
    truncated: false,
  };
}
