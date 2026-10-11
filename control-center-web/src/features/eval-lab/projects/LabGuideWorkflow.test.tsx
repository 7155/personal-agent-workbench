import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { createAgentProjection } from '@/contracts/agent-reducer';
import type { AgentWorkflowStateV1 } from '@/contracts/generated/agent-workflow-state.v1';
import { MockControlTransport } from '@/test/mock-transport';
import type { ControlRequest } from '@/platform/transport';
import { LabGuideWorkflow } from './LabGuideWorkflow';
import { agentProjectionKey, agentSessionAddress } from '@/features/agent/state/live-store';

const clients: QueryClient[] = [];
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); });
function state(configured = true): AgentWorkflowStateV1 {
  const projection = createAgentProjection('guide-session');
  return { schemaVersion: 'rag-ime.agent-workflow-state.v1', ok: true, sessionId: 'guide-session', todo: projection.todo, actGate: projection.actGate,
    goal: configured ? { ...projection.goal, sessionId: 'guide-session', configured: true, goalId: 'goal-one', revision: 1, status: 'active', objective: '继续当前研究', budget: { tokenLimit: 10000, timeLimitMs: 3600000 }, usage: { tokens: 125, elapsedMs: 60000 }, remaining: { tokens: 9875, timeMs: 3540000 } } : { ...projection.goal, sessionId: 'guide-session' } };
}
function mount(configured = true, workflowReady?: Promise<void>) {
  let current = state(configured);
  const transport = new MockControlTransport({ routes: {
    'agent.session.workflow.get': () => workflowReady ? workflowReady.then(() => current) : current,
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
  await waitFor(() => expect(screen.getByRole('button', { name: '保存预算' })).toBeDisabled());
  const client = clients.at(-1)!;
  expect(client.getQueryData<AgentWorkflowStateV1>(['agent', 'workflow', agentProjectionKey(agentSessionAddress(transport, 'guide-session'))])?.goal).toMatchObject({ revision: 2, budget: { tokenLimit: 12000 }, usage: { tokens: 125 } });
  expect(client.getQueryData(['agent', 'workflow', 'guide-session'])).toBeUndefined();
  fireEvent.change(input, { target: { value: '14000' } });
  fireEvent.click(screen.getByRole('button', { name: '保存预算' }));
  await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId.endsWith('.mutate'))).toHaveLength(2));
  expect(transport.requests.filter(({ request }) => request.pathId.endsWith('.mutate'))[1]?.request.body).toMatchObject({ expectedRevision: 2, tokenBudget: 14000 });
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

it('keeps the first explicit edit when the native budget has just become visible', async () => {
  let release!: () => void;
  const ready = new Promise<void>((resolve) => { release = resolve; });
  let edited = false;
  const observer = new MutationObserver(() => {
    const input = document.querySelector<HTMLInputElement>('form.lab-guide-budget input[type=number]');
    if (edited || !input) return;
    edited = true;
    fireEvent.change(input, { target: { value: '12000' } });
    fireEvent.click(screen.getByRole('button', { name: '保存预算' }));
  });
  observer.observe(document.body, { childList: true, subtree: true });
  try {
    const transport = mount(true, ready);
    fireEvent.click(screen.getByRole('button', { name: '项目 Agent 推进与预算' }));
    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.workflow.get')).toBe(true));
    release();
    await waitFor(() => expect(edited).toBe(true));
    await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId.endsWith('.mutate'))).toHaveLength(1));
    expect(screen.getByLabelText('Token 上限')).toHaveValue(12000);
    expect(transport.requests.find(({ request }) => request.pathId.endsWith('.mutate'))?.request.body).toMatchObject({ action: 'update', expectedRevision: 1, tokenBudget: 12000, timeBudgetMs: 3600000 });
  } finally {
    observer.disconnect();
  }
});

it('keeps both edited budget fields and their focus when only the Goal revision changes', async () => {
  const transport = mount();
  fireEvent.click(screen.getByRole('button', { name: '项目 Agent 推进与预算' }));
  const input = await screen.findByLabelText('Token 上限');
  fireEvent.change(input, { target: { value: '12000' } });
  fireEvent.change(screen.getByLabelText('时间上限（分钟）'), { target: { value: '90' } });
  input.focus();
  const client = clients.at(-1)!;
  const key = ['agent', 'workflow', agentProjectionKey(agentSessionAddress(transport, 'guide-session'))];
  const current = client.getQueryData<AgentWorkflowStateV1>(key)!;
  await act(async () => { client.setQueryData(key, { ...current, goal: { ...current.goal, revision: 2, status: 'paused' } }); await new Promise<void>((resolve) => setTimeout(resolve, 0)); });
  expect(await screen.findByRole('button', { name: '恢复' })).toBeVisible();
  expect(screen.getByLabelText('Token 上限')).toBe(input);
  expect(input).toHaveValue(12000); expect(input).toHaveFocus();
  expect(screen.getByLabelText('时间上限（分钟）')).toHaveValue(90);
  expect(screen.getByRole('button', { name: '保存预算' })).toBeEnabled();
  expect(transport.requests.some(({ request }) => request.pathId.endsWith('.mutate'))).toBe(false);
});

it('preserves an edited Token limit while syncing an untouched time limit from the native Goal', async () => {
  const transport = mount();
  fireEvent.click(screen.getByRole('button', { name: '项目 Agent 推进与预算' }));
  const input = await screen.findByLabelText('Token 上限');
  fireEvent.change(input, { target: { value: '12000' } });
  const client = clients.at(-1)!;
  const key = ['agent', 'workflow', agentProjectionKey(agentSessionAddress(transport, 'guide-session'))];
  const current = client.getQueryData<AgentWorkflowStateV1>(key)!;
  await act(async () => { client.setQueryData(key, { ...current, goal: { ...current.goal, revision: 2,
    budget: { tokenLimit: 20000, timeLimitMs: 1800000 }, remaining: { tokens: 19875, timeMs: 1740000 } } }); await new Promise<void>((resolve) => setTimeout(resolve, 0)); });
  await waitFor(() => expect(screen.getByLabelText('时间上限（分钟）')).toHaveValue(30));
  expect(screen.getByLabelText('Token 上限')).toBe(input);
  expect(input).toHaveValue(12000);
  expect(screen.getByRole('button', { name: '保存预算' })).toBeEnabled();
  expect(transport.requests.some(({ request }) => request.pathId.endsWith('.mutate'))).toBe(false);
});

it('syncs both untouched budget fields from a new native Goal receipt without submitting', async () => {
  const transport = mount();
  fireEvent.click(screen.getByRole('button', { name: '项目 Agent 推进与预算' }));
  await screen.findByLabelText('Token 上限');
  const client = clients.at(-1)!;
  const key = ['agent', 'workflow', agentProjectionKey(agentSessionAddress(transport, 'guide-session'))];
  const current = client.getQueryData<AgentWorkflowStateV1>(key)!;
  await act(async () => { client.setQueryData(key, { ...current, goal: { ...current.goal, revision: 2,
    budget: { tokenLimit: 20000, timeLimitMs: 1800000 }, remaining: { tokens: 19875, timeMs: 1740000 } } }); await new Promise<void>((resolve) => setTimeout(resolve, 0)); });
  await waitFor(() => expect(screen.getByLabelText('Token 上限')).toHaveValue(20000));
  expect(screen.getByLabelText('时间上限（分钟）')).toHaveValue(30);
  expect(screen.getByRole('button', { name: '保存预算' })).toBeDisabled();
  expect(transport.requests.some(({ request }) => request.pathId.endsWith('.mutate'))).toBe(false);
});
