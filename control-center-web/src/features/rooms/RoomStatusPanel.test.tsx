import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { createAgentProjection } from '@/contracts/agent-reducer';
import { createRoomProjection } from '@/contracts/room-reducer';
import type { AgentSessionTelemetryV1 } from '@/contracts/generated/agent-session-telemetry.v1';
import { agentProjectionKey, agentSessionAddress, useAgentLiveStore } from '@/features/agent/state/live-store';
import { MockControlTransport } from '@/test/mock-transport';
import { RoomStatusPanel } from './RoomStatusPanel';
import type { RoomSummary } from './room-types';

afterEach(() => { cleanup(); useAgentLiveStore.setState({ projections: {} }); });

it('reads participant telemetry from its transport without falling back to an unscoped same-ID Session', async () => {
  const sessionId = 'same-participant-session';
  const room: RoomSummary = {
    id: 'room-read-only', title: '工作室', status: 'active', routingPolicy: 'manual_mentions',
    moderatorParticipantId: 'participant', updatedAtMs: 1,
    participants: [{ id: 'participant', sessionId, roleId: 'worker', roleVersion: '1', displayName: '伙伴', status: 'active', ordinal: 0 }],
  };
  const a = new MockControlTransport();
  const b = new MockControlTransport();
  const c = new MockControlTransport();
  useAgentLiveStore.setState({ projections: {
    [agentProjectionKey(agentSessionAddress(a, sessionId))]: { ...createAgentProjection(sessionId), telemetry: telemetry('甲的模型') },
    [agentProjectionKey(agentSessionAddress(b, sessionId))]: { ...createAgentProjection(sessionId), telemetry: telemetry('乙的模型') },
    [sessionId]: { ...createAgentProjection(sessionId), telemetry: telemetry('未绑定模型') },
  } });
  render(<>{([[a, '甲'], [b, '乙'], [c, '丙']] as const).map(([transport, label]) => (
    <ControlTransportProvider key={label} transport={transport}>
      <section aria-label={`工作室${label}`}><RoomStatusPanel room={room} projection={createRoomProjection(room.id)} open /></section>
    </ControlTransportProvider>
  ))}</>);
  const user = userEvent.setup();
  for (const label of ['甲', '乙', '丙']) {
    const toggle = within(screen.getByRole('region', { name: `工作室${label}` })).getByRole('button', { name: /伙伴状态/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
  }
  expect(within(screen.getByRole('region', { name: '工作室甲' })).getByText(/甲的模型/)).toBeVisible();
  expect(within(screen.getByRole('region', { name: '工作室乙' })).getByText(/乙的模型/)).toBeVisible();
  expect(within(screen.getByRole('region', { name: '工作室甲' })).queryByText(/乙的模型/)).not.toBeInTheDocument();
  expect(screen.queryByText(/未绑定模型/)).not.toBeInTheDocument();
  expect(screen.getByRole('region', { name: '工作室丙' }).querySelector('.room-participant-telemetry--quiet')).toBeInTheDocument();
});

function telemetry(name: string): AgentSessionTelemetryV1 {
  const usage = { input: 10, output: 2, cacheRead: 0, cacheWrite: 0, totalTokens: 12 };
  return {
    schemaVersion: 'rag-ime.agent-session-telemetry.v1', model: { provider: 'fixture', id: name, name },
    context: { tokens: 12, contextWindow: 100, percent: 12, remainingTokens: 88,
      compactAtTokens: 80, tokensUntilCompact: 68, reserveTokens: 10, keepRecentTokens: 10, autoCompactEnabled: true },
    cumulativeUsage: usage, latestUsage: usage, latestCacheHitPercent: 0,
    isCompacting: false, compactionCount: 0, updatedAtMs: 1,
  };
}
