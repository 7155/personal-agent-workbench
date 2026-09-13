import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { createAgentProjection } from '@/contracts/agent-reducer';
import type { AgentWorkflowStateV1 } from '@/contracts/generated/agent-workflow-state.v1';
import { MockControlTransport } from '@/test/mock-transport';
import type { ControlRequest } from '@/platform/transport';
import { LabGuideWorkflow } from './LabGuideWorkflow';

const clients: QueryClient[] = [];
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); });
function state(configured = true): AgentWorkflowStateV1 {
  const projection = createAgentProjection('guide-session');
  return { schemaVersion: 'rag-ime.agent-workflow-state.v1', ok: true, sessionId: 'guide-session', todo: projection.todo, actGate: projection.actGate,
    goal: configured ? { ...projection.goal, sessionId: 'guide-session', configured: true, goalId: 'goal-one', revision: 1, status: 'active', objective: '继续当前研究', budget: { tokenLimit: 10000, timeLimitMs: 3600000 }, usage: { tokens: 125, elapsedMs: 60000 }, remaining: { tokens: 9875, timeMs: 3540000 } } : { ...projection.goal, sessionId: 'guide-session' } };
}
function mount(configured = true) {
  let current = state(configured);
  const transport = new MockControlTransport({ routes: {
    'agent.session.workflow.get': () => current,
    'agent.session.goal.mutate': ({ body }: ControlRequest) => {
      const input = body as { action: string; tokenBudget?: number | null; timeBudgetMs?: number | null };
      current = { ...current, goal: { ...current.goal, revision: current.goal.revision + 1,
        ...(input.action === 'update' ? { budget: { tokenLimit: input.tokenBudget ?? null, timeLimitMs: input.timeBudgetMs ?? null } } : { status: input.action === 'pause' ? 'paused' : 'active' }) } };
      return current;
    },
  } });
  const client = new QueryClient(); clients.push(client);
  render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}><LabGuideWorkflow sessionId="guide-session" /></ControlTransportProvider></QueryClientProvider>);
  return transport;
}
it('opens native Goal controls only on demand and never configures a missing Goal', async () => {
  const transport = mount(false);
  expect(transport.requests).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: '项目 Agent 推进与预算' }));
  expect(await screen.findByText('直接在对话中描述你想完成的事')).toBeVisible();
  expect(screen.getByText(/暂停项目 Agent 不会取消所有后台任务/)).toBeVisible();
  expect(screen.queryByLabelText('Token 上限')).not.toBeInTheDocument();
  expect(transport.requests.every(({ request }) => request.pathId === 'agent.session.workflow.get' && request.params?.sessionId === 'guide-session')).toBe(true);
});
it('changes only the same Guide native budget after explicit save without resetting usage or starting work', async () => {
  const transport = mount();
  fireEvent.click(screen.getByRole('button', { name: '项目 Agent 推进与预算' }));
  const input = await screen.findByLabelText('Token 上限');
  expect(input).toHaveValue(10000); expect(screen.getByRole('button', { name: '保存预算' })).toBeDisabled();
  expect(transport.requests.some(({ request }) => request.pathId.endsWith('.mutate'))).toBe(false);
  fireEvent.change(input, { target: { value: '12000' } });
  fireEvent.click(screen.getByRole('button', { name: '保存预算' }));
  await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId.endsWith('.mutate'))).toBe(true));
  expect(transport.requests.find(({ request }) => request.pathId.endsWith('.mutate'))?.request).toMatchObject({ params: { sessionId: 'guide-session' }, body: { action: 'update', expectedRevision: 1, tokenBudget: 12000, timeBudgetMs: 3600000 } });
  expect(transport.requests.every(({ request }) => ['agent.session.workflow.get', 'agent.session.goal.mutate'].includes(request.pathId))).toBe(true);
});
it('uses the existing same-Session pause and resume actions, without cancelling independent jobs', async () => {
  const transport = mount();
  fireEvent.click(screen.getByRole('button', { name: '项目 Agent 推进与预算' }));
  fireEvent.click(await screen.findByRole('button', { name: '暂停' }));
  fireEvent.click(await screen.findByRole('button', { name: '恢复' }));
  await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId.endsWith('.mutate'))).toHaveLength(2));
  expect(transport.requests.filter(({ request }) => request.pathId.endsWith('.mutate')).map(({ request }) => request.body)).toEqual([{ action: 'pause', expectedRevision: 1 }, { action: 'resume', expectedRevision: 2 }]);
  expect(transport.requests.every(({ request }) => request.params?.sessionId === 'guide-session')).toBe(true);
});
