import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { agentEventFixture } from '@/test/fixtures/events';
import { MockControlTransport } from '@/test/mock-transport';
import { HttpControlTransport } from '@/platform/http-transport';
import type { ControlRequest } from '@/platform/transport';
import { useAgentLiveStore } from '../state/live-store';
import { useAgentLiveSession } from './use-agent-live-session';

const SESSION_ID = 'session-shared';

afterEach(() => {
  cleanup();
  useAgentLiveStore.getState().clear(SESSION_ID);
  vi.useRealTimers();
});

describe('useAgentLiveSession shared ownership', () => {
  it('uses one snapshot and stream while two windows observe the same event', async () => {
    const transport = new MockControlTransport({
      routes: {
        'agent.session.snapshot': {
          lastSequence: 0,
          resumeToken: `${SESSION_ID}:0`,
          status: 'idle',
          messages: [],
          liveEvents: [],
        },
      },
    });
    const firstEvent = vi.fn();
    const secondEvent = vi.fn();
    const firstEvents = vi.fn();
    const secondEvents = vi.fn();

    const firstWindow = renderHook(() => useAgentLiveSession({
      sessionId: SESSION_ID,
      transport,
      onEvent: firstEvent,
      onEvents: firstEvents,
    }));
    const secondWindow = renderHook(() => useAgentLiveSession({
      sessionId: SESSION_ID,
      transport,
      onEvent: secondEvent,
      onEvents: secondEvents,
    }));

    await waitFor(() => expect(transport.subscriptionCalls).toHaveLength(1));
    expect(transport.requests.filter(({ request }) => (
      request.pathId === 'agent.session.snapshot'
    ))).toHaveLength(1);
    expect(transport.activeSubscriptionCount()).toBe(1);

    const event = agentEventFixture(1, 'text_delta', {
      delta: '共享实时回答',
      replaceBlock: true,
    });
    const rawEvent = Object.fromEntries(
      Object.entries({
        ...event,
        eventId: `${SESSION_ID}:1`,
        sessionId: SESSION_ID,
        resumeToken: `${SESSION_ID}:1`,
      }).filter(([key]) => key !== 'streamKind'),
    );
    act(() => {
      expect(transport.emit('agent.session.events', rawEvent)).toBe(1);
    });
    // Text deltas notify views after the shared batched projection commits.
    await waitFor(() => {
      expect(firstEvent).toHaveBeenCalledTimes(1);
      expect(secondEvent).toHaveBeenCalledTimes(1);
    });
    expect(firstEvent).toHaveBeenCalledWith(expect.objectContaining({
      eventId: `${SESSION_ID}:1`,
      sessionId: SESSION_ID,
    }));
    expect(secondEvent).toHaveBeenCalledWith(expect.objectContaining({
      eventId: `${SESSION_ID}:1`,
      sessionId: SESSION_ID,
    }));
    await waitFor(() => {
      expect(firstEvents).toHaveBeenCalledTimes(1);
      expect(secondEvents).toHaveBeenCalledTimes(1);
    });

    firstWindow.unmount();
    expect(transport.activeSubscriptionCount()).toBe(1);
    secondWindow.unmount();
    expect(transport.activeSubscriptionCount()).toBe(0);
  });

  it('isolates a failing window callback from the shared stream and other windows', async () => {
    const transport = new MockControlTransport({
      routes: {
        'agent.session.snapshot': {
          lastSequence: 0,
          resumeToken: `${SESSION_ID}:0`,
          status: 'idle',
          messages: [],
          liveEvents: [],
        },
      },
    });
    const healthyWindowEvent = vi.fn();
    const failingWindow = renderHook(() => useAgentLiveSession({
      sessionId: SESSION_ID,
      transport,
      onEvent: () => {
        throw new Error('window render callback failed');
      },
    }));
    const healthyWindow = renderHook(() => useAgentLiveSession({
      sessionId: SESSION_ID,
      transport,
      onEvent: healthyWindowEvent,
    }));
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));

    const event = agentEventFixture(1, 'turn_completed', { status: 'completed' });
    const rawEvent = Object.fromEntries(
      Object.entries({
        ...event,
        eventId: `${SESSION_ID}:1`,
        sessionId: SESSION_ID,
        resumeToken: `${SESSION_ID}:1`,
      }).filter(([key]) => key !== 'streamKind'),
    );
    act(() => {
      transport.emit('agent.session.events', rawEvent);
    });

    expect(healthyWindowEvent).toHaveBeenCalledWith(expect.objectContaining({
      eventId: `${SESSION_ID}:1`,
    }));
    expect(useAgentLiveStore.getState().projections[SESSION_ID]).toMatchObject({
      lastSequence: 1,
      resumeToken: `${SESSION_ID}:1`,
      status: 'idle',
    });
    expect(transport.activeSubscriptionCount()).toBe(1);
    failingWindow.unmount();
    healthyWindow.unmount();
  });

  it('recovers a dropped shared stream once for every observing window', async () => {
    let snapshotCalls = 0;
    const transport = new MockControlTransport({
      routes: {
        'agent.session.snapshot': () => {
          snapshotCalls += 1;
          return {
            lastSequence: 0,
            resumeToken: `${SESSION_ID}:0`,
            status: 'idle',
            messages: [],
            liveEvents: [],
          };
        },
      },
    });
    const firstError = vi.fn();
    const secondError = vi.fn();
    const firstRestored = vi.fn();
    const secondRestored = vi.fn();
    const firstWindow = renderHook(() => useAgentLiveSession({
      sessionId: SESSION_ID,
      transport,
      onConnectionError: firstError,
      onConnectionRestored: firstRestored,
    }));
    const secondWindow = renderHook(() => useAgentLiveSession({
      sessionId: SESSION_ID,
      transport,
      onConnectionError: secondError,
      onConnectionRestored: secondRestored,
    }));
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));
    vi.useFakeTimers();

    const interruption = new Error('stream interrupted');
    act(() => transport.fail('agent.session.events', interruption));
    expect(firstError).toHaveBeenCalledWith(SESSION_ID, interruption);
    expect(secondError).toHaveBeenCalledWith(SESSION_ID, interruption);
    expect(snapshotCalls).toBe(1);

    await act(async () => vi.advanceTimersByTimeAsync(1_250));
    await act(async () => Promise.resolve());
    expect(snapshotCalls).toBe(2);
    expect(transport.subscriptionCalls).toHaveLength(2);
    expect(transport.activeSubscriptionCount()).toBe(1);
    expect(firstRestored).toHaveBeenLastCalledWith(SESSION_ID);
    expect(secondRestored).toHaveBeenLastCalledWith(SESSION_ID);
    firstWindow.unmount();
    secondWindow.unmount();
  });

  it('does not declare an HTTP stream restored from headers alone', async () => {
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
      return new Response(JSON.stringify({
        lastSequence: 0,
        resumeToken: `${SESSION_ID}:0`,
        status: 'idle',
        messages: [],
        liveEvents: [],
      }), { headers: { 'Content-Type': 'application/json' } });
    }) as typeof fetch;
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: fetchMock,
    });
    const restored = vi.fn();
    const recoveryState = vi.fn();
    const window = renderHook(() => useAgentLiveSession({
      sessionId: SESSION_ID,
      transport,
      onConnectionRestored: restored,
      onRecoveryState: recoveryState,
    }));

    await waitFor(() => expect(streamController).toBeDefined());
    expect(restored).not.toHaveBeenCalled();
    expect(recoveryState).toHaveBeenLastCalledWith('recovering');
    streamController?.enqueue(new TextEncoder().encode(': heartbeat\n'));
    await Promise.resolve();
    expect(restored).not.toHaveBeenCalled();
    streamController?.enqueue(new TextEncoder().encode('\n'));
    await waitFor(() => expect(restored).toHaveBeenCalledTimes(1));
    expect(recoveryState).toHaveBeenLastCalledWith('synced');
    window.unmount();
  });

  it('honors a transport Retry-After hint in the single shared recovery loop', async () => {
    let snapshotCalls = 0;
    const transport = new MockControlTransport({
      routes: {
        'agent.session.snapshot': () => {
          snapshotCalls += 1;
          return {
            lastSequence: 0,
            resumeToken: `${SESSION_ID}:0`,
            status: 'idle',
            messages: [],
            liveEvents: [],
          };
        },
      },
    });
    const firstWindow = renderHook(() => useAgentLiveSession({
      sessionId: SESSION_ID,
      transport,
    }));
    const secondWindow = renderHook(() => useAgentLiveSession({
      sessionId: SESSION_ID,
      transport,
    }));
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));
    vi.useFakeTimers();

    act(() => transport.fail(
      'agent.session.events',
      Object.assign(new Error('event stream capacity'), { retryAfterMs: 2_000 }),
    ));
    await act(async () => vi.advanceTimersByTimeAsync(1_999));
    expect(snapshotCalls).toBe(1);
    await act(async () => vi.advanceTimersByTimeAsync(251));
    await act(async () => Promise.resolve());
    expect(snapshotCalls).toBe(2);
    expect(transport.activeSubscriptionCount()).toBe(1);
    firstWindow.unmount();
    secondWindow.unmount();
  });

  it('adds deterministic owner jitter instead of retrying all Sessions at one second', async () => {
    const ownerIds = ['session-jitter-alpha', 'session-jitter-beta'] as const;
    const callsByOwner = new Map<string, number>();
    const recoveryTimes: number[] = [];
    const transport = new MockControlTransport({
      routes: {
        'agent.session.snapshot': (request: ControlRequest) => {
          const ownerId = request.params?.sessionId ?? '';
          const call = (callsByOwner.get(ownerId) ?? 0) + 1;
          callsByOwner.set(ownerId, call);
          if (call > 1) recoveryTimes.push(Date.now());
          return {
            lastSequence: 0,
            resumeToken: `${ownerId}:0`,
            status: 'idle',
            messages: [],
            liveEvents: [],
          };
        },
      },
    });
    const first = renderHook(() => useAgentLiveSession({
      sessionId: ownerIds[0],
      transport,
    }));
    const second = renderHook(() => useAgentLiveSession({
      sessionId: ownerIds[1],
      transport,
    }));
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(2));
    vi.useFakeTimers();
    vi.setSystemTime(100_000);

    act(() => transport.fail('agent.session.events', new Error('gateway restart')));
    await act(async () => vi.advanceTimersByTimeAsync(1_250));
    await act(async () => Promise.resolve());

    expect(recoveryTimes).toHaveLength(2);
    expect(recoveryTimes.every((at) => at > 101_000 && at <= 101_250)).toBe(true);
    expect(new Set(recoveryTimes).size).toBe(2);
    first.unmount();
    second.unmount();
    for (const ownerId of ownerIds) useAgentLiveStore.getState().clear(ownerId);
  });

  it('does not reset recovery backoff until the recovered stream delivers an event', async () => {
    let snapshotCalls = 0;
    const transport = new MockControlTransport({
      stableOnOpen: false,
      routes: {
        'agent.session.snapshot': () => {
          snapshotCalls += 1;
          return {
            lastSequence: 0,
            resumeToken: `${SESSION_ID}:0`,
            status: 'idle',
            messages: [],
            liveEvents: [],
          };
        },
      },
    });
    const window = renderHook(() => useAgentLiveSession({
      sessionId: SESSION_ID,
      transport,
    }));
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));
    vi.useFakeTimers();

    act(() => transport.fail('agent.session.events', new Error('first early failure')));
    await act(async () => vi.advanceTimersByTimeAsync(1_250));
    await act(async () => Promise.resolve());
    expect(snapshotCalls).toBe(2);

    act(() => transport.fail('agent.session.events', new Error('second early failure')));
    await act(async () => vi.advanceTimersByTimeAsync(1_250));
    await act(async () => Promise.resolve());
    expect(snapshotCalls).toBe(2);
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    await act(async () => Promise.resolve());
    expect(snapshotCalls).toBe(3);

    const event = Object.fromEntries(Object.entries({
      ...agentEventFixture(1, 'turn_completed', { status: 'completed' }),
      eventId: `${SESSION_ID}:1`,
      sessionId: SESSION_ID,
      resumeToken: `${SESSION_ID}:1`,
    }).filter(([key]) => key !== 'streamKind'));
    act(() => {
      expect(transport.emit('agent.session.events', event)).toBe(1);
      transport.fail('agent.session.events', new Error('failure after stable event'));
    });
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(snapshotCalls).toBe(3);
    await act(async () => vi.advanceTimersByTimeAsync(250));
    await act(async () => Promise.resolve());
    expect(snapshotCalls).toBe(4);
    window.unmount();
  });

  it('hydrates the durable high-water snapshot after a transient snapshot-required control', async () => {
    let snapshotCalls = 0;
    const transport = new MockControlTransport({
      routes: {
        'agent.session.snapshot': () => {
          snapshotCalls += 1;
          return snapshotCalls === 1
            ? {
                lastSequence: 1,
                resumeToken: `${SESSION_ID}:1`,
                status: 'busy',
                messages: [],
                liveEvents: [],
                partial: true,
                snapshotScope: 'recent',
              }
            : {
                lastSequence: 41,
                resumeToken: `${SESSION_ID}:41`,
                status: 'busy',
                messages: [],
                liveEvents: [],
                partial: true,
                snapshotScope: 'recent',
              };
        },
      },
    });
    const window = renderHook(() => useAgentLiveSession({
      sessionId: SESSION_ID,
      transport,
    }));
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));

    const control = agentEventFixture(42, 'snapshot_required', {
      reason: 'event_replay_gap',
      afterEventId: `${SESSION_ID}:1`,
    });
    const rawControl = Object.fromEntries(
      Object.entries({
        ...control,
        eventId: `${SESSION_ID}:snapshot-required:41`,
        sessionId: SESSION_ID,
        resumeToken: `${SESSION_ID}:snapshot-required:41`,
      }).filter(([key]) => key !== 'streamKind'),
    );
    act(() => {
      expect(transport.emit('agent.session.events', rawControl)).toBe(1);
    });
    await waitFor(() => expect(snapshotCalls).toBe(2));
    await waitFor(() => expect(transport.subscriptionCalls).toHaveLength(2));

    expect(useAgentLiveStore.getState().projections[SESSION_ID]).toMatchObject({
      lastSequence: 41,
      resumeToken: `${SESSION_ID}:41`,
      needsSnapshot: false,
    });
    expect(transport.subscriptionCalls[1]?.request.lastEventId).toBe(`${SESSION_ID}:41`);
    window.unmount();
  });

  it('accepts an equal-cursor busy snapshot when an explicit gap requires repair', async () => {
    let snapshotCalls = 0;
    const transport = new MockControlTransport({
      routes: {
        'agent.session.snapshot': () => {
          snapshotCalls += 1;
          return {
            lastSequence: 1,
            resumeToken: `${SESSION_ID}:1`,
            status: 'busy',
            messages: [],
            liveEvents: [],
            partial: true,
            snapshotScope: 'recent',
          };
        },
      },
    });
    const window = renderHook(() => useAgentLiveSession({
      sessionId: SESSION_ID,
      transport,
    }));
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));

    const control = agentEventFixture(2, 'snapshot_required', {
      reason: 'event_replay_gap',
      afterEventId: `${SESSION_ID}:snapshot-required:1`,
    });
    const rawControl = Object.fromEntries(
      Object.entries({
        ...control,
        eventId: `${SESSION_ID}:snapshot-required:1`,
        sessionId: SESSION_ID,
        resumeToken: `${SESSION_ID}:snapshot-required:1`,
      }).filter(([key]) => key !== 'streamKind'),
    );
    act(() => {
      expect(transport.emit('agent.session.events', rawControl)).toBe(1);
    });
    await waitFor(() => expect(snapshotCalls).toBe(2));
    await waitFor(() => expect(transport.subscriptionCalls).toHaveLength(2));

    expect(useAgentLiveStore.getState().projections[SESSION_ID]).toMatchObject({
      lastSequence: 1,
      resumeToken: `${SESSION_ID}:1`,
      needsSnapshot: false,
      status: 'busy',
    });
    window.unmount();
  });

  it('hydrates an intermediate snapshot after an ordinary sequence gap', async () => {
    let snapshotCalls = 0;
    const transport = new MockControlTransport({
      routes: {
        'agent.session.snapshot': () => {
          snapshotCalls += 1;
          return snapshotCalls === 1
            ? {
                lastSequence: 1,
                resumeToken: `${SESSION_ID}:1`,
                status: 'busy',
                messages: [],
                liveEvents: [],
                partial: true,
                snapshotScope: 'recent',
              }
            : {
                lastSequence: 2,
                resumeToken: `${SESSION_ID}:2`,
                status: 'busy',
                messages: [],
                liveEvents: [],
                partial: true,
                snapshotScope: 'recent',
              };
        },
      },
    });
    const window = renderHook(() => useAgentLiveSession({ sessionId: SESSION_ID, transport }));
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));

    const gap = agentEventFixture(3, 'turn_completed', { status: 'completed' });
    const rawGap = Object.fromEntries(Object.entries({
      ...gap,
      eventId: `${SESSION_ID}:3`,
      sessionId: SESSION_ID,
      resumeToken: `${SESSION_ID}:3`,
    }).filter(([key]) => key !== 'streamKind'));
    act(() => {
      expect(transport.emit('agent.session.events', rawGap)).toBe(1);
    });

    await waitFor(() => expect(snapshotCalls).toBe(2));
    await waitFor(() => expect(transport.subscriptionCalls).toHaveLength(2));
    expect(useAgentLiveStore.getState().projections[SESSION_ID]).toMatchObject({
      lastSequence: 2,
      resumeToken: `${SESSION_ID}:2`,
      needsSnapshot: false,
    });
    expect(transport.subscriptionCalls[1]?.request.lastEventId).toBe(`${SESSION_ID}:2`);
    window.unmount();
  });

  it('clears an ordinary gap without letting an equal-cursor busy snapshot regress a terminal', async () => {
    let snapshotCalls = 0;
    const transport = new MockControlTransport({
      routes: {
        'agent.session.snapshot': () => {
          snapshotCalls += 1;
          return snapshotCalls === 1
            ? {
                lastSequence: 1,
                resumeToken: `${SESSION_ID}:1`,
                status: 'idle',
                messages: [],
                liveEvents: [],
                runtimeQuiescent: true,
              }
            : {
                lastSequence: 1,
                resumeToken: `${SESSION_ID}:1`,
                status: 'busy',
                messages: [],
                liveEvents: [],
                partial: true,
                snapshotScope: 'recent',
                runtimeQuiescent: false,
              };
        },
      },
    });
    const window = renderHook(() => useAgentLiveSession({ sessionId: SESSION_ID, transport }));
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));
    expect(useAgentLiveStore.getState().projections[SESSION_ID]?.status).toBe('idle');

    const gap = agentEventFixture(3, 'turn_started', {});
    const rawGap = Object.fromEntries(Object.entries({
      ...gap,
      eventId: `${SESSION_ID}:3`,
      sessionId: SESSION_ID,
      resumeToken: `${SESSION_ID}:3`,
    }).filter(([key]) => key !== 'streamKind'));
    act(() => {
      expect(transport.emit('agent.session.events', rawGap)).toBe(1);
    });

    await waitFor(() => expect(snapshotCalls).toBe(2));
    await waitFor(() => expect(transport.subscriptionCalls).toHaveLength(2));
    expect(useAgentLiveStore.getState().projections[SESSION_ID]).toMatchObject({
      lastSequence: 1,
      resumeToken: `${SESSION_ID}:1`,
      needsSnapshot: false,
      status: 'idle',
    });
    window.unmount();
  });


  it('applies an equal-sequence idle snapshot to settle stale running state', async () => {
    let snapshotCalls = 0;
    const delta = agentEventFixture(1, 'text_delta', {
      delta: '重启前仍显示为流式输出',
      replaceBlock: true,
    });
    const liveEvent = {
      ...delta,
      eventId: `${SESSION_ID}:1`,
      sessionId: SESSION_ID,
      resumeToken: `${SESSION_ID}:1`,
    };
    const transport = new MockControlTransport({
      routes: {
        'agent.session.snapshot': () => {
          snapshotCalls += 1;
          return snapshotCalls === 1
            ? {
                lastSequence: 1,
                resumeToken: `${SESSION_ID}:1`,
                status: 'busy',
                messages: [],
                liveEvents: [liveEvent],
                partial: true,
                snapshotScope: 'recent',
                runtimeQuiescent: false,
              }
            : {
                lastSequence: 1,
                resumeToken: `${SESSION_ID}:1`,
                status: 'idle',
                messages: [],
                liveEvents: [],
                partial: true,
                snapshotScope: 'recent',
                runtimeQuiescent: true,
              };
        },
      },
    });
    const window = renderHook(() => useAgentLiveSession({
      sessionId: SESSION_ID,
      transport,
    }));
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));
    expect(useAgentLiveStore.getState().projections[SESSION_ID]?.turnsById['turn-1']?.status)
      .toBe('running');
    vi.useFakeTimers();

    act(() => transport.fail('agent.session.events', new Error('stream interrupted')));
    await act(async () => vi.advanceTimersByTimeAsync(1_250));
    await act(async () => Promise.resolve());

    expect(snapshotCalls).toBe(2);
    expect(useAgentLiveStore.getState().projections[SESSION_ID]?.turnsById['turn-1']?.status)
      .toBe('completed');
    window.unmount();
  });
  it('keeps retrying after the first recovery snapshot fails during a Runtime restart', async () => {
    let snapshotCalls = 0;
    const transport = new MockControlTransport({
      routes: {
        'agent.session.snapshot': () => {
          snapshotCalls += 1;
          if (snapshotCalls === 2) throw new Error('gateway restarting');
          return {
            lastSequence: 0,
            resumeToken: `${SESSION_ID}:0`,
            status: 'idle',
            messages: [],
            liveEvents: [],
          };
        },
      },
    });
    const snapshotError = vi.fn();
    const restored = vi.fn();
    const window = renderHook(() => useAgentLiveSession({
      sessionId: SESSION_ID,
      transport,
      onSnapshotError: snapshotError,
      onConnectionRestored: restored,
    }));
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));
    vi.useFakeTimers();

    act(() => transport.fail('agent.session.events', new Error('stream interrupted')));
    await act(async () => vi.advanceTimersByTimeAsync(1_250));
    expect(snapshotCalls).toBe(2);
    expect(snapshotError).toHaveBeenCalledWith(expect.objectContaining({
      sessionId: SESSION_ID,
      error: expect.objectContaining({ message: 'gateway restarting' }),
    }));

    await act(async () => vi.advanceTimersByTimeAsync(2_250));
    await act(async () => Promise.resolve());
    expect(snapshotCalls).toBe(3);
    expect(transport.activeSubscriptionCount()).toBe(1);
    expect(restored).toHaveBeenLastCalledWith(SESSION_ID);
    window.unmount();
  });
});
