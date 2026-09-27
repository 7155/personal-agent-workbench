import { cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { agentProjection, useAgentLiveStore } from '../state/live-store';
import { useAgentLiveSession } from './use-agent-live-session';

const sessionId = 'session-recent-fallback';
function message(role: 'user' | 'assistant') {
  return { schemaVersion: 'rag-ime.agent-message.v1', id: `message-${role}`, sessionId,
    turnId: 'turn-1', role, status: 'completed', blocks: [], attachments: [], citations: [],
    createdAtMs: 1, completedAtMs: 2 };
}
const recent = { sessionId, status: 'idle', partial: true, snapshotScope: 'recent',
  items: [message('user')], liveEvents: [], lastSequence: 12, resumeToken: `${sessionId}:12` };
const full = { ...recent, partial: false, snapshotScope: 'full',
  items: [message('user'), message('assistant')] };

afterEach(() => {
  cleanup();
  useAgentLiveStore.getState().clear(sessionId);
});

it('automatically loads complete history when an idle recent snapshot omits the answer, including reopening', async () => {
  const read = vi.fn((request: ControlRequest) => request.query?.view === 'recent' ? recent : full);
  const transport = new MockControlTransport({ routes: { 'agent.session.snapshot': read } });
  for (let opening = 0; opening < 2; opening += 1) {
    const onSnapshot = vi.fn();
    const onSnapshotError = vi.fn();
    const onRecoveryState = vi.fn();
    const hook = renderHook(() => useAgentLiveSession({ sessionId, transport,
      onSnapshot, onSnapshotError, onRecoveryState }));
    await waitFor(() => expect(onSnapshot).toHaveBeenCalledWith(expect.objectContaining({ view: 'full', hydrated: true })));
    expect(onSnapshotError).not.toHaveBeenCalled();
    expect(onRecoveryState).toHaveBeenLastCalledWith('synced');
    expect(agentProjection(sessionId).status).toBe('idle');
    expect(agentProjection(sessionId).messagesById['message-assistant']).toBeDefined();
    expect(transport.activeSubscriptionCount()).toBe(1);
    hook.unmount();
    useAgentLiveStore.getState().clear(sessionId);
  }
  expect(read.mock.calls.map(([request]) => request.query?.view ?? 'full')).toEqual(['recent', 'full', 'recent', 'full']);
});

it('reports a failed full fallback without spinning immediate recent reads or claiming sync', async () => {
  const read = vi.fn((request: ControlRequest) => {
    if (request.query?.view === 'recent') return recent;
    throw new Error('full snapshot unavailable');
  });
  const transport = new MockControlTransport({ routes: { 'agent.session.snapshot': read } });
  const onSnapshotError = vi.fn();
  const onRecoveryState = vi.fn();
  renderHook(() => useAgentLiveSession({ sessionId, transport, onSnapshotError, onRecoveryState }));
  await waitFor(() => expect(onSnapshotError).toHaveBeenCalledWith(expect.objectContaining({ view: 'full' })));
  expect(read).toHaveBeenCalledTimes(2);
  expect(onRecoveryState.mock.calls.some(([state]) => state === 'synced')).toBe(false);
});

it('keeps the bounded recent path while the Runtime is busy', async () => {
  const transport = new MockControlTransport({ routes: {
    'agent.session.snapshot': { ...recent, status: 'busy' },
  } });
  const onSnapshot = vi.fn();
  renderHook(() => useAgentLiveSession({ sessionId, transport, onSnapshot }));
  await waitFor(() => expect(onSnapshot).toHaveBeenCalledWith(expect.objectContaining({ view: 'recent' })));
  expect(transport.requests).toHaveLength(1);
});
