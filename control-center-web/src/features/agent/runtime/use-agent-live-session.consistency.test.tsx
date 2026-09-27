import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { agentEventFixture } from '@/test/fixtures/events';
import { MockControlTransport } from '@/test/mock-transport';
import { agentProjection, useAgentLiveStore } from '../state/live-store';
import { useAgentLiveSession } from './use-agent-live-session';

const SESSION_ID = 'session-consistency';
const options = { sessionId: SESSION_ID };
function snapshot(sequence = 1) {
  return { sessionId: SESSION_ID, lastSequence: sequence, resumeToken: `${SESSION_ID}:${sequence}`,
    status: 'idle', messages: [], liveEvents: [] };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => { resolve = yes; });
  return { promise, resolve };
}
function event(sequence: number, type: string) {
  const value = agentEventFixture(sequence, type, { delta: 'visible', replaceBlock: true });
  return Object.fromEntries(Object.entries({ ...value, sessionId: SESSION_ID,
    eventId: `${SESSION_ID}:${sequence}`, resumeToken: `${SESSION_ID}:${sequence}`,
  }).filter(([key]) => key !== 'streamKind'));
}

afterEach(() => {
  cleanup();
  useAgentLiveStore.getState().clear(SESSION_ID);
  vi.useRealTimers();
});

describe('Session projection consistency', () => {
  it('resolves invalidations made during loading only after a trailing read', async () => {
    const first = deferred<ReturnType<typeof snapshot>>();
    const second = deferred<ReturnType<typeof snapshot>>();
    const read = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const transport = new MockControlTransport({ routes: { 'agent.session.snapshot': read } });
    const hook = renderHook(() => useAgentLiveSession({ ...options, transport }));
    await waitFor(() => expect(read).toHaveBeenCalledTimes(1));
    const done = vi.fn();
    let result!: Promise<boolean>;
    act(() => { result = hook.result.current(); void result.then(done); });
    await act(async () => { first.resolve(snapshot(1)); });
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    expect(done).not.toHaveBeenCalled();
    await act(async () => { second.resolve(snapshot(2)); await result; });
    expect(await result).toBe(true);
    expect(agentProjection(SESSION_ID).lastSequence).toBe(2);
  });

  it('notifies a late window of the accepted snapshot without another read or stream', async () => {
    const transport = new MockControlTransport({ routes: { 'agent.session.snapshot': snapshot() } });
    const first = vi.fn();
    renderHook(() => useAgentLiveSession({ ...options, transport, onSnapshot: first }));
    await waitFor(() => expect(first).toHaveBeenCalledTimes(1));
    const late = vi.fn();
    renderHook(() => useAgentLiveSession({ ...options, transport, onSnapshot: late }));
    expect(late).toHaveBeenCalledTimes(1);
    expect(late.mock.calls[0][0]).toMatchObject({ hydrated: true, sequence: 1 });
    expect(transport.requests).toHaveLength(1);
    expect(transport.activeSubscriptionCount()).toBe(1);
  });

  it('notifies views only once for an already committed terminal event', async () => {
    const transport = new MockControlTransport({ routes: { 'agent.session.snapshot': snapshot() } });
    const notify = vi.fn();
    renderHook(() => useAgentLiveSession({ ...options, transport, onEvent: notify }));
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));
    act(() => {
      transport.emit('agent.session.events', event(2, 'turn_completed'));
      transport.emit('agent.session.events', event(2, 'turn_completed'));
    });
    expect(notify).toHaveBeenCalledTimes(1);
    expect(agentProjection(SESSION_ID).lastSequence).toBe(2);
  });

  it('does not let a gap-causing terminal event run view side effects', async () => {
    const repair = deferred<ReturnType<typeof snapshot>>();
    const read = vi.fn().mockResolvedValueOnce(snapshot()).mockReturnValue(repair.promise);
    const transport = new MockControlTransport({ routes: { 'agent.session.snapshot': read } });
    const notify = vi.fn();
    renderHook(() => useAgentLiveSession({ ...options, transport, onEvent: notify }));
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));
    act(() => { transport.emit('agent.session.events', event(3, 'turn_completed')); });
    expect(notify).not.toHaveBeenCalled();
    expect(agentProjection(SESSION_ID)).toMatchObject({ lastSequence: 1, needsSnapshot: true });
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
  });

  it('lets a delta callback read the text from the already committed projection', async () => {
    const transport = new MockControlTransport({ routes: { 'agent.session.snapshot': snapshot() } });
    const cursors: number[] = [];
    renderHook(() => useAgentLiveSession({ ...options, transport,
      onEvent: () => { cursors.push(agentProjection(SESSION_ID).lastSequence); },
    }));
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));
    act(() => { transport.emit('agent.session.events', event(2, 'text_delta')); });
    await waitFor(() => expect(cursors).toEqual([2]));
  });

  it('flushes received deltas before a refresh that may return an older cursor', async () => {
    const transport = new MockControlTransport({ routes: { 'agent.session.snapshot': snapshot() } });
    const hook = renderHook(() => useAgentLiveSession({ ...options, transport }));
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));
    let result!: Promise<boolean>;
    await act(async () => {
      transport.emit('agent.session.events', event(2, 'text_delta'));
      result = hook.result.current();
      await result;
    });
    expect(agentProjection(SESSION_ID).lastSequence).toBe(2);
    expect(await result).toBe(false);
  });

  it('does not advertise a stale response as a hydrated snapshot', async () => {
    const later = deferred<ReturnType<typeof snapshot>>();
    const read = vi.fn().mockResolvedValueOnce(snapshot()).mockReturnValueOnce(later.promise);
    const transport = new MockControlTransport({ routes: { 'agent.session.snapshot': read } });
    const notify = vi.fn();
    const hook = renderHook(() => useAgentLiveSession({ ...options, transport, onSnapshot: notify }));
    await waitFor(() => expect(notify).toHaveBeenCalledTimes(1));
    let result!: Promise<boolean>;
    act(() => { result = hook.result.current(); });
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    await act(async () => {
      useAgentLiveStore.getState().hydrate(SESSION_ID, snapshot(3));
      later.resolve(snapshot(2));
      await result;
    });
    expect(await result).toBe(false);
    expect(notify).toHaveBeenCalledTimes(1);
    expect(agentProjection(SESSION_ID).lastSequence).toBe(3);
  });

  it('reports an unsuccessful snapshot read as false even if its stream can reopen', async () => {
    const read = vi.fn().mockResolvedValueOnce(snapshot()).mockRejectedValueOnce(new Error('offline'));
    const transport = new MockControlTransport({ routes: { 'agent.session.snapshot': read } });
    const hook = renderHook(() => useAgentLiveSession({ ...options, transport }));
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));
    let result!: Promise<boolean>;
    await act(async () => { result = hook.result.current(); await result; });
    expect(await result).toBe(false);
  });
});
