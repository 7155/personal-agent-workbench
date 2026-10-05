import { cleanup, render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import { createAgentProjection, type AgentProjectionState } from '@/contracts/agent-reducer';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { MockControlTransport } from '@/test/mock-transport';
import { agentProjectionKey, agentSessionAddress, useAgentLiveStore } from '../state/live-store';
import { AgentStatusPanel, CurrentTurnTaskPlan, projectStatusPanel } from './AgentStatusPanel';

afterEach(() => { cleanup(); useAgentLiveStore.setState({ projections: {} }); });

it('keeps same-ID status projections inside their own transport', async () => {
  const sessionId = 'same-status-session';
  const a = new MockControlTransport();
  const b = new MockControlTransport();
  const projected = (title: string): AgentProjectionState => ({
    ...createAgentProjection(sessionId),
    messageQueue: { steering: [title], followUp: [] },
  });
  useAgentLiveStore.setState({ projections: {
    [agentProjectionKey(agentSessionAddress(a, sessionId))]: projected('甲的待处理消息'),
    [agentProjectionKey(agentSessionAddress(b, sessionId))]: projected('乙的待处理消息'),
    [sessionId]: projected('未绑定的待处理消息'),
  } });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<TooltipProvider><QueryClientProvider client={client}>
    {([[a, '甲'], [b, '乙']] as const).map(([transport, label]) => (
      <ControlTransportProvider key={label} transport={transport}>
        <section aria-label={`状态${label}`}><AgentStatusPanel sessionId={sessionId} open keepContentMounted
          surfaceActive={false} onClose={() => {}} commands={[]} tools={[]} toolCatalogStatus="ready" busy={false}
          onCapabilityPreferenceChange={() => {}} onCapabilityPolicyRetry={() => {}} onCapabilityCatalogRetry={() => {}} />
        </section>
      </ControlTransportProvider>
    ))}
  </QueryClientProvider></TooltipProvider>);
  // Opening defers the body through animation frames and idle work.
  expect(await within(screen.getByRole('region', { name: '状态甲' })).findByText('甲的待处理消息')).toBeVisible();
  expect(await within(screen.getByRole('region', { name: '状态乙' })).findByText('乙的待处理消息')).toBeVisible();
  expect(within(screen.getByRole('region', { name: '状态甲' })).queryByText('乙的待处理消息')).not.toBeInTheDocument();
  expect(screen.queryByText('未绑定的待处理消息')).not.toBeInTheDocument();
});

describe('CurrentTurnTaskPlan', () => {
  it('keeps a compact current window but makes every task-plan step reachable', async () => {
    const user = userEvent.setup();
    const tasks = Array.from({ length: 13 }, (_, index) => ({
      id: `task-${index + 1}`,
      label: `计划步骤 ${index + 1}`,
      status: index === 0 ? 'running' : 'queued',
    }));
    render(<CurrentTurnTaskPlan tasks={tasks} />);

    const summary = screen.getByText('本轮计划').closest('summary') as HTMLElement;
    expect(summary).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('当前 6 / 共 13 项')).toBeInTheDocument();
    expect(screen.queryByText('计划步骤 7')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '显示更多计划步骤：7 项' }));
    expect(screen.getByText('当前 13 / 共 13 项')).toBeInTheDocument();
    expect(screen.getByText('计划步骤 13')).toBeInTheDocument();
    await user.click(summary);
    expect(summary).toHaveAttribute('aria-expanded', 'false');
    const reveal = document.getElementById(summary.getAttribute('aria-controls')!);
    expect(reveal).toHaveAttribute('aria-hidden', 'true');
    expect(reveal).toHaveAttribute('inert');
    // Shared Disclosure retains the content through its exit transition rather
    // than mechanically removing a long plan at the instant of closing.
    expect(screen.getByText('计划步骤 13')).toBeInTheDocument();
  });

  it('does not truncate the structured task plan during projection', () => {
    const items = Array.from({ length: 13 }, (_, index) => ({
      title: `投影步骤 ${index + 1}`,
      status: index === 12 ? 'completed' : 'queued',
    }));
    const projection = {
      turnOrder: ['turn-plan'],
      turnsById: {
        'turn-plan': { id: 'turn-plan', status: 'running', messageIds: ['message-plan'], activityIds: [] },
      },
      messagesById: {
        'message-plan': {
          id: 'message-plan',
          attachments: [],
          blocks: [{ id: 'plan-block', type: 'task_plan', data: { items } }],
        },
      },
      activitiesById: {},
    } as unknown as AgentProjectionState;

    const view = projectStatusPanel(projection);
    expect(view.tasks).toHaveLength(13);
    expect(view.tasks.at(-1)).toMatchObject({ label: '投影步骤 13', status: 'completed' });
  });
});
