import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { agentEventFixture } from '@/test/fixtures/events';
import { MockControlTransport } from '@/test/mock-transport';
import { useAgentLiveSession } from './use-agent-live-session';
import { useAgentLiveStore, agentProjection } from '../state/live-store';

const sessionId = 'session-1';
function snapshot(lastSequence: number) {
  return { sessionId, lastSequence, resumeToken: `${sessionId}:${lastSequence}`, status: 'idle', messages: [], liveEvents: [] };
}
function wire<T extends { streamKind?: unknown }>(value: T) { const { streamKind: _, ...rest } = value; return rest; }
function reset(cursor: number) {
  return wire({ ...agentEventFixture(cursor + 1, 'snapshot_required', { reason: 'event_replay_gap', afterEventId: 'session-1:10' }),
    turnId: '', eventId: `${sessionId}:snapshot-required:${cursor}`, resumeToken: `${sessionId}:snapshot-required:${cursor}` });
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(yes => { resolve = yes; });
  return { promise, resolve };
}
async function flush() { await act(async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); }); }
afterEach(() => { cleanup(); useAgentLiveStore.getState().clear(sessionId); vi.useRealTimers(); });

it('repairs an authoritative lower cursor after a replay reset and resumes its next event', async () => {
  vi.useFakeTimers(); let cursor = 10;
  const transport = new MockControlTransport({ routes: { 'agent.session.snapshot': () => snapshot(cursor) } });
  const onEvents = vi.fn();
  renderHook(() => useAgentLiveSession({ sessionId, transport, onEvents })); await flush();
  cursor = 5; act(() => { transport.emit('agent.session.events', reset(5)); }); await flush();
  expect(agentProjection(sessionId)).toMatchObject({ lastSequence: 5, needsSnapshot: false, resumeToken: 'session-1:5' });
  expect(transport.subscriptionCalls.at(-1)?.request.lastEventId).toBe('session-1:5');
  expect(transport.requests.at(-1)?.request.query).toBeUndefined();
  act(() => { transport.emit('agent.session.events', wire(agentEventFixture(6, 'text_delta', { delta: 'after restore' }))); });
  await act(async () => { await vi.advanceTimersByTimeAsync(100); });
  expect(agentProjection(sessionId).lastSequence).toBe(6); expect(onEvents).toHaveBeenCalledTimes(1);
});

it('ordinary delayed full history cannot rewind a healthy Session or swallow the next event', async () => {
  vi.useFakeTimers(); let cursor = 10;
  const transport = new MockControlTransport({ routes: { 'agent.session.snapshot': () => snapshot(cursor) } });
  const hook = renderHook(() => useAgentLiveSession({ sessionId, transport })); await flush();
  cursor = 5; await act(async () => { await hook.result.current({ view: 'full' }); });
  expect(agentProjection(sessionId)).toMatchObject({ lastSequence: 10, needsSnapshot: false });
  act(() => { transport.emit('agent.session.events', wire(agentEventFixture(11, 'text_delta', { delta: 'newer' }))); });
  await act(async () => { await vi.advanceTimersByTimeAsync(100); });
  expect(agentProjection(sessionId).lastSequence).toBe(11);
});

it('does not let a snapshot below the explicit reset high-water mark clear recovery', async () => {
  vi.useFakeTimers(); let cursor = 10;
  const transport = new MockControlTransport({ routes: { 'agent.session.snapshot': () => snapshot(cursor) } });
  const hook = renderHook(() => useAgentLiveSession({ sessionId, transport })); await flush();
  cursor = 4; act(() => { transport.emit('agent.session.events', reset(5)); }); await flush();
  expect(agentProjection(sessionId)).toMatchObject({ lastSequence: 10, needsSnapshot: true });
  cursor = 5; await act(async () => { await hook.result.current({ view: 'full' }); });
  expect(agentProjection(sessionId)).toMatchObject({ lastSequence: 5, needsSnapshot: false });
});

it.each([5, 10, 20])('a full history response at %i started before a reset is not its replacement authority', async oldSequence => {
  vi.useFakeTimers(); const old = deferred<ReturnType<typeof snapshot>>(); const replacement = deferred<ReturnType<typeof snapshot>>();
  const read = vi.fn().mockResolvedValueOnce(snapshot(10)).mockReturnValueOnce(old.promise).mockReturnValueOnce(replacement.promise);
  const transport = new MockControlTransport({ routes: { 'agent.session.snapshot': read } });
  const hook = renderHook(() => useAgentLiveSession({ sessionId, transport })); await flush();
  act(() => { void hook.result.current({ view: 'full' }); }); await flush();
  act(() => { transport.emit('agent.session.events', reset(5)); }); await flush();
  await act(async () => { old.resolve({ ...snapshot(oldSequence), ...(oldSequence === 10 ? { status: 'busy' } : {}) }); }); await flush();
  expect(read).toHaveBeenCalledTimes(3);
  expect(agentProjection(sessionId)).toMatchObject({ lastSequence: 10, needsSnapshot: true });
  await act(async () => { replacement.resolve(snapshot(5)); }); await flush();
  expect(agentProjection(sessionId)).toMatchObject({ lastSequence: 5, needsSnapshot: false });
});
