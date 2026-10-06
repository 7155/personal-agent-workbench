import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import type { ControlTransport } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { AgentTurn } from '../timeline/AgentTimeline';
import { useAgentLiveStore } from '../state/live-store';
import { useAgentLiveSession } from './use-agent-live-session';

const sessionId = 'same-session-id';
const turnId = 'same-turn-id';
afterEach(() => { cleanup(); useAgentLiveStore.setState({ projections: {} }); });

function snapshot(text: string) {
  return { sessionId, snapshotScope: 'recent', partial: true, lastSequence: 0, resumeToken: `${sessionId}:0`, status: 'busy', liveEvents: [],
    messages: [{ schemaVersion: 'rag-ime.agent-message.v1', id: 'same-message-id', sessionId, turnId,
      role: 'assistant', status: 'streaming', createdAtMs: 1, completedAtMs: null, attachments: [], citations: [],
      blocks: [{ id: 'same-text-block', type: 'text', status: 'running', presentationKind: 'markdown', data: { text } }] }],
  };
}
function transport(text: string) {
  const value = new MockControlTransport({ routes: { 'agent.session.snapshot': snapshot(text) } });
  // Distinct stream owners can point at the same endpoint. Endpoint text must
  // not collapse their in-memory projection lifetimes into one owner.
  Object.defineProperty(value, 'connectionIdentity', { value: 'http:same-endpoint' });
  return value;
}
function View({ transport, label }: { transport: ControlTransport; label: string }) {
  return <ControlTransportProvider transport={transport}><TooltipProvider><section aria-label={label}>
    <ObservedTurn transport={transport} />
  </section></TooltipProvider></ControlTransportProvider>;
}
function ObservedTurn({ transport }: { transport: ControlTransport }) {
  useAgentLiveSession({ sessionId, transport });
  return <AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => undefined} />;
}
function delta(text: string) {
  return { schemaVersion: 'rag-ime.agent-event.v1', eventId: `${sessionId}:1`, sessionId, turnId, sequence: 1, createdAtMs: 2,
    eventType: 'text_delta', payload: { messageId: 'same-message-id', blockId: 'same-text-block', delta: text }, resumeToken: `${sessionId}:1` };
}

describe('transport-scoped Agent projection', () => {
  it('renders different snapshots for two transports sharing all protocol identities', async () => {
    const a = transport('甲连接的独立回答'); const b = transport('乙连接的独立回答');
    render(<><View transport={a} label="连接甲" /><View transport={b} label="连接乙" /></>);
    await waitFor(() => { expect(a.activeSubscriptionCount()).toBe(1); expect(b.activeSubscriptionCount()).toBe(1); });
    expect(within(screen.getByRole('region', { name: '连接甲' })).getByText('甲连接的独立回答')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: '连接乙' })).getByText('乙连接的独立回答')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: '连接甲' })).queryByText('乙连接的独立回答')).not.toBeInTheDocument();
  });

  it('keeps equal-sequence deltas in their own transport instead of leaking or deduplicating them across connections', async () => {
    const a = transport(''); const b = transport('');
    render(<><View transport={a} label="流甲" /><View transport={b} label="流乙" /></>);
    await waitFor(() => { expect(a.activeSubscriptionCount()).toBe(1); expect(b.activeSubscriptionCount()).toBe(1); });
    act(() => { a.emit('agent.session.events', delta('只属于甲的片段')); });
    await within(screen.getByRole('region', { name: '流甲' })).findByText('只属于甲的片段');
    expect(within(screen.getByRole('region', { name: '流乙' })).queryByText('只属于甲的片段')).not.toBeInTheDocument();
    act(() => { b.emit('agent.session.events', delta('只属于乙的片段')); });
    await within(screen.getByRole('region', { name: '流乙' })).findByText('只属于乙的片段');
    expect(within(screen.getByRole('region', { name: '流甲' })).queryByText('只属于乙的片段')).not.toBeInTheDocument();
  });
});
