import { afterEach, describe, expect, it } from 'vitest';
import { agentSessionAddress, selectAgentProjection, useAgentLiveStore } from '@/features/agent/state/live-store';
import { MockControlTransport } from '@/test/mock-transport';
import { sendProjectGuideMessage } from './ProjectGuide';
import type { LabProject } from './types';

afterEach(() => {
  useAgentLiveStore.setState({ projections: {} });
  sessionStorage.clear();
});

const sessionId = 'shared-guide-session';
const project = { projectId: 'shared-project', guideSessionId: sessionId } as LabProject;

describe('ProjectGuide transport isolation', () => {
  it('chooses delivery only from the original transport projection, without a raw-key fallback', async () => {
    const transportA = new MockControlTransport({ routes: { 'agent.session.prompt': { accepted: true } } });
    const transportB = new MockControlTransport({ routes: { 'agent.session.prompt': { accepted: true } } });
    const store = useAgentLiveStore.getState();
    store.appendOptimistic(agentSessionAddress(transportA, sessionId), { clientMessageId: 'active-a', text: 'Running A', nowMs: 1 });
    store.appendOptimistic(agentSessionAddress(null, sessionId), { clientMessageId: 'legacy-active', text: 'Legacy fixture', nowMs: 1 });

    await sendProjectGuideMessage(transportB, project, 'First B message', 'message-b');
    await sendProjectGuideMessage(transportA, project, 'Steer A message', 'message-a');

    expect(transportB.requests[0].request).toMatchObject({ params: { sessionId }, body: { delivery: 'prompt' } });
    expect(transportA.requests[0].request).toMatchObject({ params: { sessionId }, body: { delivery: 'steer' } });
    expect(selectAgentProjection(useAgentLiveStore.getState(), agentSessionAddress(transportB, sessionId))?.messageOrder).toHaveLength(1);
  });

  it.each(['accepted', 'cancelled', 'ambiguous'] as const)('keeps a late %s receipt with its captured transport and client identity', async (outcome) => {
    let resolve!: (value: unknown) => void;
    let reject!: (reason: unknown) => void;
    const receipt = new Promise((onResolve, onReject) => { resolve = onResolve; reject = onReject; });
    const transportA = new MockControlTransport({ routes: { 'agent.session.prompt': () => receipt } });
    const transportB = new MockControlTransport();
    const addressA = agentSessionAddress(transportA, sessionId);
    const addressB = agentSessionAddress(transportB, sessionId);
    const clientMessageId = 'same-client-id';
    const sent = sendProjectGuideMessage(transportA, project, 'Original A message', clientMessageId);
    const settled = sent.catch((reason: unknown) => reason);
    useAgentLiveStore.getState().appendOptimistic(addressB, { clientMessageId, text: 'Independent B message', nowMs: 2 });
    const beforeB = selectAgentProjection(useAgentLiveStore.getState(), addressB);

    if (outcome === 'ambiguous') reject(new TypeError('fetch failed'));
    else resolve(outcome === 'cancelled'
      ? { accepted: false, cancelled: true, admissionCancelled: true }
      : { accepted: true });
    await settled;

    expect(selectAgentProjection(useAgentLiveStore.getState(), addressB)).toBe(beforeB);
    const afterA = selectAgentProjection(useAgentLiveStore.getState(), addressA)!;
    const localA = Object.values(afterA.messagesById)[0];
    if (outcome === 'cancelled') expect(afterA.messageOrder).toEqual([]);
    else expect(localA).toMatchObject(outcome === 'ambiguous'
      ? { clientMessageId, status: 'failed', admissionState: 'ambiguous' }
      : { clientMessageId, status: 'queued' });
    expect(transportA.requests).toHaveLength(1);
    expect(transportB.requests).toHaveLength(0);
    expect(useAgentLiveStore.getState().projections[sessionId]).toBeUndefined();
  });
});
