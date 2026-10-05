import { afterEach, describe, expect, it } from 'vitest';
import { MockControlTransport } from '@/test/mock-transport';
import { agentEventFixture } from '@/test/fixtures/events';
import { agentProjection, agentProjectionKey, agentSessionAddress, selectAgentProjection, useAgentLiveStore } from './live-store';

const sessionId = 'shared-id';
afterEach(() => useAgentLiveStore.setState({ projections: {} }));
function addressPair() {
  const a = new MockControlTransport(); const b = new MockControlTransport();
  Object.defineProperty(a, 'connectionIdentity', { value: 'http:same-origin' });
  Object.defineProperty(b, 'connectionIdentity', { value: 'http:same-origin' });
  return { a, b, first: agentSessionAddress(a, sessionId), second: agentSessionAddress(b, sessionId) };
}
function event(sequence: number, delta: string) {
  return { ...agentEventFixture(sequence, 'text_delta', { messageId: 'assistant', blockId: 'text', delta }), sessionId, resumeToken: `${sessionId}:${sequence}` };
}

describe('Agent store transport addresses', () => {
  it('reuses one address for one transport while keeping endpoint twins and legacy fixtures separate', () => {
    const { a, first, second } = addressPair();
    expect(agentSessionAddress(a, sessionId)).toBe(first);
    expect(agentProjectionKey(first)).not.toBe(agentProjectionKey(second));
    useAgentLiveStore.getState().appendOptimistic(sessionId, { clientMessageId: 'legacy', text: 'legacy fixture', nowMs: 1 });
    expect(selectAgentProjection(useAgentLiveStore.getState(), first)).toBeUndefined();
    useAgentLiveStore.getState().ensure(first);
    expect(agentProjection(first).sessionId).toBe(sessionId);
    expect(agentProjection(first).messageOrder).toEqual([]);
  });

  it('isolates equal event sequences and each connection’s optimistic admission and Stop receipt', () => {
    const { first, second } = addressPair(); const store = useAgentLiveStore.getState();
    store.applyEvents(first, [event(1, '甲')]); store.applyEvents(second, [event(1, '乙')]);
    expect(agentProjection(first).messagesById.assistant.blocks[0].data.text).toBe('甲');
    expect(agentProjection(second).messagesById.assistant.blocks[0].data.text).toBe('乙');
    store.appendOptimistic(first, { clientMessageId: 'same-client', text: '甲的请求', nowMs: 2 });
    store.appendOptimistic(second, { clientMessageId: 'same-client', text: '乙的请求', nowMs: 2 });
    store.failOptimistic(first, 'same-client', '回执未确认', 3, 'ambiguous');
    expect(agentProjection(first).messagesById['local:same-client'].admissionState).toBe('ambiguous');
    expect(agentProjection(second).messagesById['local:same-client'].admissionState).not.toBe('ambiguous');
    store.abortTurn(first, 'turn-1', 4);
    expect(agentProjection(first).turnsById['turn-1'].status).toBe('aborted');
    expect(agentProjection(second).turnsById['turn-1'].status).toBe('running');
    store.clear(first);
    expect(selectAgentProjection(useAgentLiveStore.getState(), first)).toBeUndefined();
    expect(agentProjection(second).messagesById.assistant.blocks[0].data.text).toBe('乙');
  });

  it('keeps a recovery gap and a rejected snapshot local to its address', () => {
    const { first, second } = addressPair(); const store = useAgentLiveStore.getState();
    store.applyEvents(first, [event(1, '甲')]); store.applyEvents(second, [event(1, '乙')]);
    store.applyEvents(first, [event(3, '跳过一条')]);
    expect(agentProjection(first).needsSnapshot).toBe(true);
    expect(agentProjection(second).needsSnapshot).toBe(false);
    expect(store.hydrate(first, { sessionId: 'another-session', messages: [], liveEvents: [], lastSequence: 5 })).toBe(false);
    expect(agentProjection(second).lastSequence).toBe(1);
  });
});
