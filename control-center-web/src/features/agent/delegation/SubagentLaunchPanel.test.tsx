import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { StubControlTransport } from '@/test/stub-control-transport';
import { SubagentLaunchPanel } from './SubagentLaunchPanel';

afterEach(() => {
  cleanup();
  document.querySelector('meta[name="paw-deployment"]')?.remove();
});

describe('SubagentLaunchPanel', () => {
  it('keeps the reviewer read-only and launches a real Pi fork with a structured contract', async () => {
    const transport = new StubControlTransport('mock', {
      'agent.subagents.templates': {
        schemaVersion: 'rag-ime.agent-template-list.v1',
        ok: true,
        maxParallel: 2,
        maxDepth: 2,
        items: [
          template('worker', '执行者', 'write', ['read_only', 'write']),
          template('reviewer', '审阅者', 'read_only', ['read_only']),
        ],
      },
      'agent.tools.list': {
        ok: true,
        items: [tool('workspace', '工作区'), tool('knowledge', '知识检索')],
      },
      'agent.subagents.create': {
        ok: true,
        accepted: true,
        batch: { id: 'batch:1', runs: [{ id: 'run:1' }] },
      },
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(
      <ControlTransportProvider transport={transport}>
        <QueryClientProvider client={queryClient}>
          <SubagentLaunchPanel
            parents={[{
              sessionId: 'session:root',
              label: 'Root',
              canWrite: true,
              workspaceRoots: ['/workspace'],
              piSkillsEnabled: true,
              codexSkillsEnabled: false,
            }]}
          />
        </QueryClientProvider>
      </ControlTransportProvider>,
    );

    await user.click(await screen.findByText('审阅者'));
    expect(screen.getByRole('radio', { name: '工作区写入' })).toBeDisabled();
    await user.click(screen.getByText('工具与技能'));
    expect(screen.getByRole('checkbox', { name: /Pi Skills/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /Codex Skills/ })).not.toBeChecked();
    await user.click(screen.getByRole('radio', { name: 'Fork 当前' }));
    await user.type(screen.getByRole('textbox', { name: '子 Agent 有界任务' }), '审查当前实现');
    await user.type(screen.getByRole('textbox', { name: '子 Agent 预期交付' }), '列出证据与风险');
    await user.type(screen.getByRole('textbox', { name: '子 Agent 验收条件' }), '不修改文件');
    await user.click(screen.getByRole('button', { name: '启动子 Agent' }));

    await screen.findByText(/1 个子 Agent 已排队/);
    const request = transport.requests.find((item) => item.pathId === 'agent.subagents.create');
    expect(request?.body).toEqual(expect.objectContaining({
      sessionId: 'session:root',
      agent: 'reviewer',
      contextMode: 'fork',
      forkEntryId: 'latest',
      access: 'read_only',
      workspaceRoots: ['/workspace'],
      wait: false,
      outputSchema: expect.objectContaining({
        required: ['summary', 'evidenceRefs', 'residualRisks'],
      }),
    }));
    expect(request?.body).not.toHaveProperty('piSkillsEnabled');
    expect(request?.body).not.toHaveProperty('codexSkillsEnabled');
  });

  it('lets a worker narrow tools without widening the parent workspace boundary', async () => {
    const transport = new StubControlTransport('mock', {
      'agent.subagents.templates': {
        ok: true,
        items: [template('worker', '执行者', 'write', ['read_only', 'write'])],
      },
      'agent.tools.list': {
        ok: true,
        items: [tool('workspace', '工作区'), tool('knowledge', '知识检索')],
      },
      'agent.subagents.create': {
        ok: true,
        batch: { runs: [{ id: 'run:worker' }] },
      },
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(
      <ControlTransportProvider transport={transport}>
        <QueryClientProvider client={queryClient}>
          <SubagentLaunchPanel parents={[{
            sessionId: 'session:root',
            label: 'Root',
            canWrite: true,
            workspaceRoots: ['/workspace'],
          }]} />
        </QueryClientProvider>
      </ControlTransportProvider>,
    );

    await screen.findByRole('radio', { name: /^执行者/ });
    await user.click(screen.getByText('工具与技能'));
    await user.click(screen.getByRole('radio', { name: '手动收窄' }));
    await user.click(await screen.findByText('知识检索'));
    await user.type(screen.getByRole('textbox', { name: '子 Agent 有界任务' }), '实现一个小改动');
    await user.type(screen.getByRole('textbox', { name: '子 Agent 预期交付' }), '补丁与测试回执');
    await user.type(screen.getByRole('textbox', { name: '子 Agent 验收条件' }), '只改授权目录');
    await user.click(screen.getByRole('button', { name: '启动子 Agent' }));

    await waitFor(() => expect(transport.requests.some((item) => item.pathId === 'agent.subagents.create')).toBe(true));
    const request = transport.requests.find((item) => item.pathId === 'agent.subagents.create');
    expect(request?.body).toEqual(expect.objectContaining({
      agent: 'worker',
      access: 'write',
      workspaceRoots: ['/workspace'],
      allowedTools: ['knowledge'],
    }));
  });

  it('removes the host Codex Skills switch and sends an explicit disabled value in Team mode', async () => {
    const teamMeta = document.createElement('meta');
    teamMeta.name = 'paw-deployment';
    teamMeta.content = 'team';
    document.head.appendChild(teamMeta);
    const transport = new StubControlTransport('mock', {
      'agent.subagents.templates': {
        ok: true,
        items: [template('worker', '执行者', 'write', ['read_only', 'write'])],
      },
      'agent.tools.list': {
        ok: true,
        items: [tool('workspace', '工作区')],
      },
      'agent.subagents.create': {
        ok: true,
        batch: { runs: [{ id: 'run:team-worker' }] },
      },
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(
      <ControlTransportProvider transport={transport}>
        <QueryClientProvider client={queryClient}>
          <SubagentLaunchPanel parents={[{
            sessionId: 'session:team',
            label: 'Team Root',
            canWrite: true,
            workspaceRoots: ['/workspace'],
            piSkillsEnabled: true,
            codexSkillsEnabled: true,
          }]} />
        </QueryClientProvider>
      </ControlTransportProvider>,
    );

    await user.click(screen.getByText('工具与技能'));
    expect(screen.queryByRole('checkbox', { name: /Codex Skills/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('checkbox', { name: /Pi Skills/ })).not.toBeInTheDocument();
    expect(screen.getByText('应用与 Skills 继承父任务的固定版本')).toBeInTheDocument();
    await user.type(screen.getByRole('textbox', { name: '子 Agent 有界任务' }), '只使用团队工作区');
    await user.type(screen.getByRole('textbox', { name: '子 Agent 预期交付' }), '交付摘要');
    await user.type(screen.getByRole('textbox', { name: '子 Agent 验收条件' }), '不读取本机技能');
    await user.click(screen.getByRole('button', { name: '启动子 Agent' }));

    await screen.findByText(/1 个子 Agent 已排队/);
    const request = transport.requests.find((item) => item.pathId === 'agent.subagents.create');
    expect(request?.body).toMatchObject({ codexSkillsEnabled: false });
    expect(request?.body).not.toHaveProperty('piSkillsEnabled');
  });

  it('defaults a Room-bound Tool Agent to inherited write access and allows an explicit narrow launch', async () => {
    const transport = new StubControlTransport('mock', {
      'agent.subagents.templates': {
        ok: true,
        items: [template('worker', '执行者', 'read_only', ['read_only', 'write'])],
      },
      'agent.subagents.create': {
        ok: true,
        batch: { runs: [{ id: 'run:room-worker' }] },
      },
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(
      <ControlTransportProvider transport={transport}>
        <QueryClientProvider client={queryClient}>
          <SubagentLaunchPanel
            availableTools={[]}
            parents={[{
              sessionId: 'session:room-partner',
              label: 'Earth',
              canWrite: true,
              defaultAccess: 'write',
              accessPolicyDetail: '卫星 / Tool Agent 配置 继承（Inherit），生效 全自动，继承行星 / Partner',
              workspaceRoots: ['/workspace'],
            }]}
            surface="room"
          />
        </QueryClientProvider>
      </ControlTransportProvider>,
    );

    expect(await screen.findByRole('radio', { name: '工作区写入' })).toBeChecked();
    expect(screen.getByText(/卫星 \/ Tool Agent 配置 继承（Inherit），生效 全自动/)).toBeInTheDocument();
    await user.click(screen.getByRole('radio', { name: '只读' }));
    await user.type(screen.getByRole('textbox', { name: '子 Agent 有界任务' }), '只读核对实现');
    await user.type(screen.getByRole('textbox', { name: '子 Agent 预期交付' }), '核对结论');
    await user.type(screen.getByRole('textbox', { name: '子 Agent 验收条件' }), '不得写入');
    await user.click(screen.getByRole('button', { name: '启动子 Agent' }));

    await waitFor(() => expect(
      transport.requests.some((item) => item.pathId === 'agent.subagents.create'),
    ).toBe(true));
    expect(
      transport.requests.find((item) => item.pathId === 'agent.subagents.create')?.body,
    ).toEqual(expect.objectContaining({
      sessionId: 'session:room-partner',
      access: 'read_only',
    }));
  });

  it('keeps the standalone Session launch on the template default and disables Room escalation', async () => {
    const standaloneTransport = new StubControlTransport('mock', {
      'agent.subagents.templates': {
        ok: true,
        items: [template('worker', '执行者', 'read_only', ['read_only', 'write'])],
      },
    });
    const standaloneClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { unmount } = render(
      <ControlTransportProvider transport={standaloneTransport}>
        <QueryClientProvider client={standaloneClient}>
          <SubagentLaunchPanel
            availableTools={[]}
            parents={[{
              sessionId: 'session:standalone',
              label: 'Standalone',
              canWrite: true,
              defaultAccess: 'write',
            }]}
          />
        </QueryClientProvider>
      </ControlTransportProvider>,
    );

    expect(await screen.findByRole('radio', { name: '只读' })).toBeChecked();
    expect(screen.getByRole('radio', { name: '工作区写入' })).toBeEnabled();
    unmount();

    const roomTransport = new StubControlTransport('mock', {
      'agent.subagents.templates': {
        ok: true,
        items: [template('worker', '执行者', 'write', ['read_only', 'write'])],
      },
    });
    const roomClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <ControlTransportProvider transport={roomTransport}>
        <QueryClientProvider client={roomClient}>
          <SubagentLaunchPanel
            availableTools={[]}
            parents={[{
              sessionId: 'session:bounded-room',
              label: 'Mars',
              canWrite: false,
              defaultAccess: 'read_only',
              accessPolicyDetail: '卫星 / Tool Agent 生效 只读',
            }]}
            surface="room"
          />
        </QueryClientProvider>
      </ControlTransportProvider>,
    );

    expect(await screen.findByRole('radio', { name: '只读' })).toBeChecked();
    expect(screen.getByRole('radio', { name: '工作区写入' })).toBeDisabled();
  });
});

function template(
  templateId: 'worker' | 'reviewer',
  displayName: string,
  defaultAccess: 'read_only' | 'write',
  allowedAccess: ('read_only' | 'write')[],
) {
  return {
    schemaVersion: 'rag-ime.agent-template.v1',
    templateId,
    version: '1',
    displayName,
    summary: templateId === 'reviewer' ? '独立核对，不修改实现。' : '在授权工作区内执行。',
    contextModes: ['fresh', 'fork'],
    toolProfileVersion: templateId === 'reviewer' ? 'subagent-readonly-v1' : 'subagent-worker-v1',
    defaultAccess,
    allowedAccess,
    budget: {
      maxDepth: 2,
      maxTurns: 0,
      maxToolCalls: 0,
      maxTotalTokens: 32_000,
      maxDurationMs: 300_000,
      maxOutputChars: 12_000,
    },
    capabilities: templateId === 'reviewer' ? ['review'] : ['control'],
  };
}

function tool(id: string, displayName: string) {
  return {
    schemaVersion: 'rag-ime.control-tool-manifest.v1',
    id,
    domain: id,
    displayName,
    description: `${displayName}说明`,
    category: id === 'workspace' ? 'workspace' : 'knowledge',
    riskLevel: 'R0',
    sessionModes: ['assistant', 'coordinator'],
    operations: ['read'],
    resultPresentation: 'tool_result',
    availability: 'online',
    version: '1',
  };
}
