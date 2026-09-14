import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it } from 'vitest';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { ScenarioAgentPolicySettings } from './ScenarioAgentPolicySettings';

afterEach(cleanup);

const scenarios = [
  ['ordinary', '普通 Agent', ['Agent']],
  ['room', 'Room 协作', ['Room']],
  ['trace', 'Trace 诊断评测', ['Trace Agent']],
  ['agentLab', 'Agent Lab', ['Agent Lab', 'Lab App']],
] as const;

function response(revision: number, policies: Record<string, { promptInstructions: string; toolAllowlist: string[] }>) {
  return {
    ok: true,
    configuration: { revision, configuration: { scenarioPolicies: policies } },
    scenarioPolicyCatalog: {
      schemaVersion: 'rag-ime.agent-scenario-policy-catalog.v1',
      policyRevision: 'rag-ime.agent-scenario-policy.v1',
      scenarios: scenarios.map(([id, label, appIds]) => ({
        id, label, description: `${label}说明`, appIds,
        variantModes: id === 'agentLab' ? ['project-guide', 'room', 'application'] : ['assistant'],
        builtInToolIds: ['overview', 'workspace_read'],
        ...policies[id],
      })),
    },
  };
}

it('shows scenario policy state and saves a scoped Lab prompt/tool change', async () => {
  const user = userEvent.setup();
  let revision = 4;
  const policies = Object.fromEntries(scenarios.map(([id]) => [id, { promptInstructions: '', toolAllowlist: ['overview', 'workspace_read'] }])) as Record<string, { promptInstructions: string; toolAllowlist: string[] }>;
  const writes: ControlRequest[] = [];
  const transport = new MockControlTransport({
    routes: {
      'agent.configuration.get': () => response(revision, policies),
      'agent.configuration.update': (request) => {
        writes.push(request);
        const body = request.body as { expectedRevision: number; changes: Record<string, { promptInstructions: string; toolAllowlist: string[] }> };
        expect(body.expectedRevision).toBe(revision);
        policies.agentLab = body.changes['scenarioPolicies.agentLab']!;
        revision += 1;
        return response(revision, policies);
      },
    },
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><ScenarioAgentPolicySettings routeIds={['agent.configuration.get', 'agent.configuration.update']} transport={transport} /></QueryClientProvider>);

  const tablist = await screen.findByRole('tablist', { name: 'App 和场景策略' });
  await user.click(within(tablist).getByRole('tab', { name: /Agent Lab/ }));
  const panel = screen.getByRole('tabpanel', { name: /Agent Lab/ });
  expect(within(panel).getByText(/Agent Lab、Lab App/)).toBeInTheDocument();
  await user.type(within(panel).getByLabelText('App/场景系统提示词补充'), '输出来源和验证状态。');
  await user.click(within(panel).getByRole('button', { name: '保存 Agent Lab' }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0]?.body).toMatchObject({ expectedRevision: 4, changes: { 'scenarioPolicies.agentLab': { promptInstructions: '输出来源和验证状态。', toolAllowlist: ['overview', 'workspace_read'] } } });
  expect(await within(panel).findByText('场景策略已保存')).toBeInTheDocument();
});
