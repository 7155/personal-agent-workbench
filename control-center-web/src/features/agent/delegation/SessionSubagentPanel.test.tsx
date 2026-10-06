import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import type { AgentSubagentRunV1 } from '@/contracts/generated/agent-subagent-run.v1';
import { StubControlTransport } from '@/test/stub-control-transport';
import { SessionSubagentPanel } from './SessionSubagentPanel';

afterEach(cleanup);

describe('SessionSubagentPanel', () => {
  it('isolates same-ID run lists between connections using the shared status-list owner', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const make = (label: string) => new StubControlTransport('mock', {
      'agent.subagents.list': { tree: { schemaVersion: 'rag-ime.agent-subagent-tree.v1', rootSessionId: 'session:root', nodeCount: 1, maxDepth: 1,
        roots: [{ run: { ...subagentRun('failed', label), id: `run:${label}`, task: label }, children: [] }] } },
    });
    render(<TooltipProvider><QueryClientProvider client={client}>
      {(['甲连接独有任务', '乙连接独有任务'] as const).map(label => <ControlTransportProvider key={label} transport={make(label)}>
        <section aria-label={label}><SessionSubagentPanel open onClose={() => {}} sessionId="session:root" tools={[]} /></section>
      </ControlTransportProvider>)}
    </QueryClientProvider></TooltipProvider>);
    for (const label of ['甲连接独有任务', '乙连接独有任务']) {
      expect(await within(screen.getByRole('region', { name: label })).findByRole('list', { name: '子 Agent 节点' })).toHaveTextContent(label);
    }
  });

  it('shows the concrete failure reason on failed and timed-out graph nodes', async () => {
    const failed = subagentRun('failed', 'workspace permission denied');
    const timedOut = subagentRun('timed_out', 'child run exceeded its deadline');
    const transport = new StubControlTransport('mock', {
      'agent.subagents.list': {
        tree: {
          schemaVersion: 'rag-ime.agent-subagent-tree.v1',
          rootSessionId: 'session:root',
          nodeCount: 2,
          maxDepth: 1,
          roots: [
            { run: failed, children: [] },
            { run: timedOut, children: [] },
          ],
        },
      },
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <ControlTransportProvider transport={transport}>
        <QueryClientProvider client={queryClient}>
          <TooltipProvider>
            <SessionSubagentPanel
              open
              onClose={() => undefined}
              sessionId="session:root"
              tools={[]}
            />
          </TooltipProvider>
        </QueryClientProvider>
      </ControlTransportProvider>,
    );

    const nodes = await screen.findByRole('list', { name: '子 Agent 节点' });
    expect(nodes).toHaveTextContent(failed.error);
    expect(nodes).toHaveTextContent(timedOut.error);
  });
});

function subagentRun(
  state: AgentSubagentRunV1['state'],
  error: string,
): AgentSubagentRunV1 {
  return {
    schemaVersion: 'rag-ime.agent-subagent-run.v1',
    id: `run:${state}`,
    nodeId: `node:${state}`,
    attemptId: `attempt:${state}`,
    attemptNumber: 1,
    predecessorAttemptId: '',
    ownerRunId: 'session:root',
    parentRunId: '',
    depth: 1,
    batchId: 'batch:root',
    childSessionId: `session:child:${state}`,
    todoTask: '',
    todoPhase: '',
    templateId: 'worker',
    templateVersion: '1',
    ordinal: 0,
    task: `${state} task`,
    expectedOutput: '公开失败原因',
    acceptanceCriteria: [],
    launchDigest: {
      schemaVersion: 'rag-ime.agent-subagent-launch-digest.v1',
      contextMode: 'fresh',
      templateId: 'worker',
      templateVersion: '1',
      modelProfile: 'test/model',
      thinkingLevel: 'medium',
      toolProfileVersion: 'subagent-worker-v1',
      toolAllowlistMode: 'profile',
      tools: ['read'],
      piSkillsEnabled: false,
      codexSkillsEnabled: false,
      workspaceAccess: 'read_only',
      workspaceRootCount: 0,
      outputContract: { required: false, schemaSha256: '' },
      extensionRuntime: 'pi_host_managed',
    },
    contract: { status: 'not_requested', error: '', toolCallId: '', validatedAtMs: null },
    state,
    budget: { maxTurns: 1, maxToolCalls: 1, maxTotalTokens: 1_000, maxDurationMs: 1_000, maxOutputChars: 1_000 },
    usage: { turnCount: 1, toolCount: 0, totalTokens: 1 },
    result: {},
    error,
    resultContextScheduledAtMs: null,
    createdAtMs: 1,
    startedAtMs: 1,
    updatedAtMs: 2,
    completedAtMs: 2,
  };
}
