import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { preloadRecentConversations } from '@/features/conversation-ui/conversation-preload';
import { MockControlTransport } from '@/test/mock-transport';
import { agentEventFixture } from '@/test/fixtures/events';
import { agentProjection, agentSessionAddress, selectAgentProjection, useAgentLiveStore } from '../state/live-store';
import { useAgentLiveSession } from './use-agent-live-session';

const SESSION_ID = 'reopened-session';
const RETAINED_SETTLED_LIMIT = 24;
function snapshot(text = 'Confirmed answer') {
  return {
    sessionId: SESSION_ID, status: 'idle', lastSequence: 2, resumeToken: `${SESSION_ID}:2`,
    snapshotScope: 'recent', partial: true, runtimeQuiescent: true,
    messages: [{ schemaVersion: 'rag-ime.agent-message.v1', id: 'answer', sessionId: SESSION_ID,
      turnId: 'turn-1', role: 'assistant', status: 'completed',
      blocks: [{ id: 'answer:text', type: 'text', status: 'completed', presentationKind: 'markdown', data: { text } }],
      attachments: [], citations: [], createdAtMs: 20, completedAtMs: 21 }], liveEvents: [],
  };
}
function transportFor(text?: string) {
  return new MockControlTransport({ routes: { 'agent.session.snapshot': snapshot(text) } });
}
async function flush() {
  await act(async () => { for (let i = 0; i < 15; i += 1) await Promise.resolve(); });
}
async function cycleSettledTransports(count = RETAINED_SETTLED_LIMIT + 8) {
  const addresses = [];
  for (let index = 0; index < count; index += 1) {
    const transport = transportFor(`Answer ${index}`);
    const address = agentSessionAddress(transport, SESSION_ID);
    const view = renderHook(() => useAgentLiveSession({ sessionId: SESSION_ID, transport }));
    await flush();
    expect(agentProjection(address).messagesById.answer.blocks[0].data.text).toBe(`Answer ${index}`);
    expect(transport.activeSubscriptionCount()).toBe(1);
    view.unmount();
    expect(transport.activeSubscriptionCount()).toBe(0);
    addresses.push(address);
  }
  return addresses;
}

afterEach(() => {
  cleanup();
  useAgentLiveStore.setState({ projections: {} });
});

describe('inactive Agent projection retention', () => {
  it('bounds settled history after repeated mount, snapshot, unmount, and new transport with the same Session ID', async () => {
    const addresses = await cycleSettledTransports();
    expect(Object.keys(useAgentLiveStore.getState().projections)).toHaveLength(RETAINED_SETTLED_LIMIT);
    expect(selectAgentProjection(useAgentLiveStore.getState(), addresses[0])).toBeUndefined();
    expect(agentProjection(addresses.at(-1)!).messagesById.answer.blocks[0].data.text).toBe('Answer 31');
  });

  it('also bounds preload-only history without ever acquiring a live lease', async () => {
    for (let index = 0; index < RETAINED_SETTLED_LIMIT + 8; index += 1) {
      const transport = transportFor(`Preloaded ${index}`);
      await preloadRecentConversations(transport, [{ kind: 'session', id: SESSION_ID }]).promise;
      expect(transport.subscriptionCalls).toHaveLength(0);
    }
    expect(Object.keys(useAgentLiveStore.getState().projections)).toHaveLength(RETAINED_SETTLED_LIMIT);
  });

  it('does not treat default idle or an unconfirmed partial snapshot as terminal evidence', async () => {
    const unknown = agentSessionAddress(transportFor(), SESSION_ID);
    const partial = agentSessionAddress(transportFor(), SESSION_ID);
    const reset = agentSessionAddress(transportFor(), SESSION_ID);
    useAgentLiveStore.getState().ensure(unknown);
    useAgentLiveStore.getState().hydrate(partial, { ...snapshot(), runtimeQuiescent: undefined });
    useAgentLiveStore.getState().hydrate(reset, snapshot());
    useAgentLiveStore.getState().applyEvents(reset, [{
      ...agentEventFixture(2, 'snapshot_required', {}), sessionId: SESSION_ID,
      resumeToken: `${SESSION_ID}:snapshot-required:1`,
    }]);
    expect(useAgentLiveStore.getState().hydrate(reset, {
      sessionId: SESSION_ID, messages: [], liveEvents: [], lastSequence: 1,
    }, { recoveryCursor: 1 })).toBe(true);
    await cycleSettledTransports();
    expect(selectAgentProjection(useAgentLiveStore.getState(), unknown)).toBeDefined();
    expect(selectAgentProjection(useAgentLiveStore.getState(), partial)).toBeDefined();
    expect(selectAgentProjection(useAgentLiveStore.getState(), reset)).toBeDefined();
  });

  it('pins a settled projection until the last of two shared consumers leaves', async () => {
    const transport = transportFor('Keep both windows');
    const address = agentSessionAddress(transport, SESSION_ID);
    const first = renderHook(() => useAgentLiveSession({ sessionId: SESSION_ID, transport }));
    const second = renderHook(() => useAgentLiveSession({ sessionId: SESSION_ID, transport }));
    await flush();
    const projection = agentProjection(address);
    await cycleSettledTransports();
    expect(agentProjection(address)).toBe(projection);
    first.unmount();
    await cycleSettledTransports();
    expect(agentProjection(address)).toBe(projection);
    expect(transport.activeSubscriptionCount()).toBe(1);
    second.unmount();
    expect(agentProjection(address)).toBe(projection);
    await cycleSettledTransports();
    expect(selectAgentProjection(useAgentLiveStore.getState(), address)).toBeUndefined();
  });

  it('retains a short reopen and refreshes its position before later cache pressure', async () => {
    const transport = transportFor('Quick reopen');
    const address = agentSessionAddress(transport, SESSION_ID);
    const first = renderHook(() => useAgentLiveSession({ sessionId: SESSION_ID, transport }));
    await flush();
    first.unmount();
    await cycleSettledTransports(RETAINED_SETTLED_LIMIT - 1);
    const projection = agentProjection(address);
    const reopened = renderHook(() => useAgentLiveSession({ sessionId: SESSION_ID, transport }));
    expect(agentProjection(address)).toBe(projection);
    await flush();
    reopened.unmount();
    await cycleSettledTransports(1);
    expect(selectAgentProjection(useAgentLiveStore.getState(), address)).toBeDefined();
    expect(transport.requests).toHaveLength(1);
  });

  it('reloads evicted history on reopen instead of treating the recent request cache as a live projection', async () => {
    const transport = transportFor('Recover from Runtime');
    const address = agentSessionAddress(transport, SESSION_ID);
    const first = renderHook(() => useAgentLiveSession({ sessionId: SESSION_ID, transport }));
    await flush();
    first.unmount();
    await cycleSettledTransports();
    expect(selectAgentProjection(useAgentLiveStore.getState(), address)).toBeUndefined();
    renderHook(() => useAgentLiveSession({ sessionId: SESSION_ID, transport }));
    await flush();
    expect(agentProjection(address).messagesById.answer.blocks[0].data.text).toBe('Recover from Runtime');
    expect(transport.requests).toHaveLength(2);
    expect(transport.activeSubscriptionCount()).toBe(1);
  });

  it.each(['pending', 'unresolved', 'ambiguous'] as const)('preserves an inactive %s admission and its late receipt in the original transport', async (admissionState) => {
    const transport = transportFor();
    const address = agentSessionAddress(transport, SESSION_ID);
    const view = renderHook(() => useAgentLiveSession({ sessionId: SESSION_ID, transport }));
    await flush();
    act(() => {
      useAgentLiveStore.getState().appendOptimistic(address, {
        clientMessageId: 'same-client', text: 'Original request', nowMs: 30, delivery: 'followUp',
      });
      useAgentLiveStore.getState().failOptimistic(address, 'same-client', 'Receipt unknown', 31, admissionState);
    });
    view.unmount();
    await cycleSettledTransports();
    expect(agentProjection(address).messagesById['local:same-client'].admissionState).toBe(admissionState);

    const replacement = agentSessionAddress(transportFor(), SESSION_ID);
    useAgentLiveStore.getState().appendOptimistic(replacement, {
      clientMessageId: 'same-client', text: 'Replacement request', nowMs: 40, delivery: 'followUp',
    });
    const before = agentProjection(replacement);
    useAgentLiveStore.getState().acknowledgeOptimistic(address, 'same-client', 50);
    expect(agentProjection(address).messagesById['local:same-client'].deliveryState).toBe('accepted');
    expect(agentProjection(replacement)).toBe(before);
    expect(agentProjection(replacement).messagesById['local:same-client'].deliveryState).toBe('sending');
  });

  it('keeps an inactive running turn until its exact terminal event settles it', async () => {
    const transport = transportFor();
    const address = agentSessionAddress(transport, SESSION_ID);
    const view = renderHook(() => useAgentLiveSession({ sessionId: SESSION_ID, transport }));
    await flush();
    const event = (sequence: number, type: string, payload: Record<string, unknown>) => ({
      ...agentEventFixture(sequence, type, payload), sessionId: SESSION_ID, resumeToken: `${SESSION_ID}:${sequence}`,
    });
    act(() => { useAgentLiveStore.getState().applyEvents(address, [event(3, 'text_delta', { delta: 'Running' })]); });
    view.unmount();
    await cycleSettledTransports();
    expect(agentProjection(address).turnsById['turn-1'].status).toBe('running');
    useAgentLiveStore.getState().applyEvents(address, [event(4, 'turn_completed', { status: 'completed' })]);
    await cycleSettledTransports();
    expect(selectAgentProjection(useAgentLiveStore.getState(), address)).toBeUndefined();
  });

  it('preserves recovery gaps, pending delivery, and paused native work under settled-cache pressure', async () => {
    const gap = agentSessionAddress(transportFor(), SESSION_ID);
    const queued = agentSessionAddress(transportFor(), SESSION_ID);
    const paused = agentSessionAddress(transportFor(), SESSION_ID);
    const store = useAgentLiveStore.getState();
    store.hydrate(gap, snapshot());
    store.applyEvents(gap, [{ ...agentEventFixture(4, 'heartbeat', {}), sessionId: SESSION_ID }]);
    store.hydrate(queued, { ...snapshot(), messageQueue: { steering: ['waiting-message'], followUp: [] } });
    store.hydrate(paused, { ...snapshot(), runtimeEngine: 'durable', projectionCurrent: true,
      paused: true, recoverable: true, activeTurn: { turnId: 'native-turn', clientMessageId: 'native-client' },
    });
    await cycleSettledTransports();
    expect(agentProjection(gap).needsSnapshot).toBe(true);
    expect(agentProjection(queued).messageQueue.steering).toEqual(['waiting-message']);
    expect(agentProjection(paused).durableRecovery).toMatchObject({ paused: true, recoverable: true });
  });

  it('protects an inactive background job until the completion receipt arrives', async () => {
    const address = agentSessionAddress(transportFor(), SESSION_ID);
    const job = {
      schemaVersion: 'rag-ime.agent-background-job.v1', jobId: `bg_${'1'.repeat(32)}`, sessionId: SESSION_ID,
      label: 'Build', status: 'running', command: 'echo build', commandSha256: 'a'.repeat(64),
      cwd: '/workspace', networkAllowed: false, maxRunSeconds: 60, pid: 123,
      createdAtMs: 1, startedAtMs: 2, updatedAtMs: 3, endedAtMs: 0, exitCode: null,
      outputBytes: 0, logStartCursor: 0, logTruncated: false, cancelRequestedAtMs: 0,
      error: '', approvalId: '',
      causalMetadata: { todoId: '', todoRevision: 0, goalId: '', goalRevision: 0, turnId: '', roomBound: false },
    };
    useAgentLiveStore.getState().hydrate(address, { ...snapshot(), backgroundJobs: [job] });
    await cycleSettledTransports();
    expect(agentProjection(address).backgroundJobsById[job.jobId].status).toBe('running');
    expect(useAgentLiveStore.getState().applyBackgroundJobReceipt(address, {
      job: { ...job, status: 'completed', updatedAtMs: 4, endedAtMs: 4, exitCode: 0 },
    })).toBe(true);
    await cycleSettledTransports();
    expect(selectAgentProjection(useAgentLiveStore.getState(), address)).toBeUndefined();
  });
});
