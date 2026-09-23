import { afterEach, describe, expect, it } from 'vitest';
import { agentProjection, useAgentLiveStore } from './live-store';

const sessionId = 'session-hydration-ack';
const snapshot = (lastSequence: number) => ({ sessionId, lastSequence,
  resumeToken: `${sessionId}:${lastSequence}`, status: 'idle', messages: [], liveEvents: [] });
afterEach(() => useAgentLiveStore.getState().clear(sessionId));

describe('snapshot commit acknowledgement', () => {
  it('returns true only when it commits the response', () => {
    expect(useAgentLiveStore.getState().hydrate(sessionId, snapshot(4))).toBe(true);
    expect(useAgentLiveStore.getState().hydrate(sessionId, snapshot(3))).toBe(false);
    expect(agentProjection(sessionId).lastSequence).toBe(4);
  });
  it('rejects a foreign response before parsing away the Session identity', () => {
    expect(useAgentLiveStore.getState().hydrate(sessionId, { ...snapshot(4), sessionId: 'other' })).toBe(false);
    expect(agentProjection(sessionId).lastSequence).toBe(0);
  });
  it('rejects a failure-shaped response instead of projecting it as an empty success', () => {
    expect(useAgentLiveStore.getState().hydrate(sessionId, { ...snapshot(4), ok: false })).toBe(false);
    expect(agentProjection(sessionId).lastSequence).toBe(0);
  });
});
