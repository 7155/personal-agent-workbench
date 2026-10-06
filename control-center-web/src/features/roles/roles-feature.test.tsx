import { readFileSync } from 'node:fs';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { MockControlTransport } from '@/test/mock-transport';
import { StubControlTransport } from '@/test/stub-control-transport';
import { previewPersonas, previewTemplates } from '@/features/agent/preview-data';
import { ModelRoutingPanel, RolesFeature } from './index';
import { agentModelRouting, roleModelCatalog } from './role-model';

describe('Roles experience', () => {
  afterEach(cleanup);
  it('preserves a dirty model route while refreshing clean routes from a newer revision', async () => {
    const user = userEvent.setup();
    const catalog = roleModelCatalog({ providers: [{ id: 'gpt', displayName: 'GPT', models: [{ provider: 'gpt', id: 'ui-model', name: '公开模型夹具', reasoning: true, thinkingLevels: ['high'] }] }] });
    const content = (revision: number, toolModel = 'inherit') => <TooltipProvider><ModelRoutingPanel catalog={catalog} routing={agentModelRouting(modelRoutingConfiguration(revision, { modelProfile: toolModel, thinkingLevel: toolModel === 'inherit' ? 'inherit' : 'high' }))} saving="" onSave={async () => {}} onOpenSettings={() => {}} /></TooltipProvider>;
    const view = render(content(1));
    await user.click(screen.getByRole('combobox', { name: '默认主 Agent默认模型' }));
    await user.click(screen.getByRole('option', { name: '公开模型夹具 · GPT' }));
    view.rerender(content(1));
    expect(screen.getByRole('combobox', { name: '默认主 Agent默认模型' })).toHaveTextContent('公开模型夹具');
    view.rerender(content(2, 'gpt/ui-model'));
    expect(screen.getByRole('combobox', { name: '默认主 Agent默认模型' })).toHaveTextContent('公开模型夹具');
    expect(screen.getByRole('combobox', { name: '私有 Tool Agent默认模型' })).toHaveTextContent('公开模型夹具');
  });
  it('makes model routing primary while keeping companion identity as migration compatibility', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.role.models': {
        ok: true,
        providers: [{
          id: 'gpt',
          displayName: 'GPT',
          models: [{ provider: 'gpt', id: 'gpt-5.6-sol', name: 'GPT-5.6 Sol', api: 'responses', reasoning: true, thinkingLevels: ['off', 'max'], supportsImages: true, contextWindow: 1000000, maxTokens: 128000 }],
        }],
      },
      'agent.configuration.get': modelRoutingConfiguration(1),
    } });
    render(<MemoryRouter><ControlTransportProvider transport={transport}><TooltipProvider><RolesFeature /></TooltipProvider></ControlTransportProvider></MemoryRouter>);
    expect(await screen.findByRole('heading', { name: '模型与扩展', level: 1 })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '按职责选择默认模型', level: 2 })).toBeInTheDocument();
    expect(screen.getByText('默认主 Agent')).toBeInTheDocument();
    expect(screen.getByText('Trace 主 Agent')).toBeInTheDocument();
    expect(screen.getByText('私有 Tool Agent')).toBeInTheDocument();
    expect(screen.getByText('私有调研卫星')).toBeInTheDocument();
    expect(screen.getByText('Room 行星伙伴')).toBeInTheDocument();
    expect(screen.getByLabelText('私有 Tool Agent默认模型')).toBeInTheDocument();
    expect(screen.getByLabelText('Trace 主 Agent默认模型')).toBeInTheDocument();
    expect(screen.getByLabelText('Room 行星伙伴默认模型')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '插件管理' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '兼容伙伴资料', level: 2 })).toBeInTheDocument();
    expect(await screen.findByText(previewPersonas[0]!.tagline)).toBeInTheDocument();
    expect(screen.getByText('适合交给她')).toBeInTheDocument();
    expect(screen.getByText('不建议交给她')).toBeInTheDocument();
    expect(screen.getByText('多伙伴主持和独立验收')).toBeInTheDocument();
    expect(screen.queryByText('多 Agent 主持和独立验收')).not.toBeInTheDocument();
    expect(screen.getAllByText('内置伙伴').length).toBeGreaterThan(0);
    expect(screen.getByText('默认')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '复制为我的伙伴' })).toBeInTheDocument();
    expect(screen.queryByText('control-center-safe-v1')).not.toBeInTheDocument();
    expect(screen.queryByText('control-center-v1')).not.toBeInTheDocument();
    expect(screen.queryByText('任务助手')).not.toBeInTheDocument();
    expect(screen.queryByText('Room 岗位')).not.toBeInTheDocument();
    expect(screen.queryByText('协作规则')).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByText('新对话设置'));
    expect((await screen.findAllByText('GPT-5.6 Sol · GPT')).length).toBeGreaterThan(0);
    await waitFor(() => expect(transport.requests.map((call) => call.request.pathId)).toEqual(expect.arrayContaining(['agent.roles.list', 'agent.role.models', 'agent.configuration.get'])));
    expect(transport.requests.some((call) => call.request.pathId === 'agent.subagents.templates')).toBe(false);
  });

  it('saves a Tool Agent model route without changing the companion default', async () => {
    const user = userEvent.setup();
    const initial = modelRoutingConfiguration(11);
    const updated = modelRoutingConfiguration(12, {
      modelProfile: 'openai-codex/gpt-5.6-terra',
      thinkingLevel: 'high',
    });
    const transport = new MockControlTransport({ routes: {
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.role.models': {
        ok: true,
        providers: [{
          id: 'openai-codex',
          displayName: 'OpenAI Codex',
          models: [
            { provider: 'openai-codex', id: 'gpt-5.6-luna', name: 'GPT-5.6 Luna', api: 'responses', reasoning: true, thinkingLevels: ['off', 'low', 'medium'], supportsImages: true, contextWindow: 128000, maxTokens: 32000 },
            { provider: 'openai-codex', id: 'gpt-5.6-terra', name: 'GPT-5.6 Terra', api: 'responses', reasoning: true, thinkingLevels: ['off', 'medium', 'high'], supportsImages: true, contextWindow: 128000, maxTokens: 32000 },
          ],
        }],
      },
      'agent.configuration.get': initial,
      'agent.configuration.update': updated,
    } });
    render(<MemoryRouter><ControlTransportProvider transport={transport}><TooltipProvider><RolesFeature /></TooltipProvider></ControlTransportProvider></MemoryRouter>);

    await screen.findByText('配置 #11');
    await user.click(screen.getByLabelText('私有 Tool Agent默认模型'));
    await user.click(await screen.findByRole('option', { name: 'GPT-5.6 Terra · OpenAI Codex' }));
    await user.click(screen.getByLabelText('私有 Tool Agent默认推理强度'));
    await user.click(await screen.findByRole('option', { name: '高' }));
    await user.click(screen.getByRole('button', { name: '保存私有 Tool Agent模型分工' }));

    await waitFor(() => expect(transport.requests.find((call) => call.request.pathId === 'agent.configuration.update')?.request.body).toEqual({
      expectedRevision: 11,
      changes: {
        'modelRouting.toolAgent': {
          modelProfile: 'openai-codex/gpt-5.6-terra',
          thinkingLevel: 'high',
        },
      },
      updatedBy: 'models-ui',
    }));
    expect(await screen.findByText('配置 #12')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('私有 Tool Agent的默认模型已保存');
  });

  it('makes Room membership a capability while keeping task jobs dynamic', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.roles.list': { ok: true, items: previewPersonas },
    } });
    render(<MemoryRouter><ControlTransportProvider transport={transport}><TooltipProvider><RolesFeature /></TooltipProvider></ControlTransportProvider></MemoryRouter>);

    expect(await screen.findByText('Room 行星伙伴')).toBeInTheDocument();
    expect(screen.getByText(/用户可见的行星伙伴；参与协作、分派工作并组织验收/)).toBeInTheDocument();
    expect(screen.queryByText('协作配置')).not.toBeInTheDocument();
    expect(screen.queryByText('协作主持')).not.toBeInTheDocument();
    expect(await screen.findByText('内置伙伴 · 复制后可以调整')).toBeInTheDocument();
  });

  it('takes the user to settings when no reasoning model is available', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: {
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.role.models': { ok: true, providers: [] },
    } });
    render(<MemoryRouter initialEntries={['/roles']}><ControlTransportProvider transport={transport}><TooltipProvider><RolesFeature /><LocationProbe /></TooltipProvider></ControlTransportProvider></MemoryRouter>);

    await user.click(await screen.findByText('新对话设置'));
    expect(await screen.findByText('当前没有可用的推理模型。请先完成模型配置，再回来设置新对话默认值。')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '打开设置' }));
    expect(screen.getByTestId('location')).toHaveTextContent('/configuration');
  });

  it('keeps new-conversation settings mounted while its disclosure closes', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: {
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.role.models': { ok: true, providers: [] },
    } });
    render(<MemoryRouter><ControlTransportProvider transport={transport}><TooltipProvider><RolesFeature /></TooltipProvider></ControlTransportProvider></MemoryRouter>);

    const summary = (await screen.findByText('新对话设置')).closest('summary');
    if (!summary) throw new Error('新对话设置 disclosure 缺少 summary');
    const details = summary.closest('details');
    if (!details) throw new Error('新对话设置 disclosure 缺少 details');
    await user.click(summary);
    const emptyMessage = await screen.findByText('当前没有可用的推理模型。请先完成模型配置，再回来设置新对话默认值。');
    expect(details).toHaveAttribute('open');
    expect(summary).toHaveAttribute('aria-expanded', 'true');
    await user.click(summary);
    expect(summary).toHaveAttribute('aria-expanded', 'false');
    expect(emptyMessage).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText('当前没有可用的推理模型。请先完成模型配置，再回来设置新对话默认值。')).not.toBeInTheDocument();
      expect(details).not.toHaveAttribute('open');
    });
  });

  it('keeps the partner-setting audit available through its closing transition', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: {
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.role.models': { ok: true, providers: [] },
    } });
    render(<MemoryRouter><ControlTransportProvider transport={transport}><TooltipProvider><RolesFeature /></TooltipProvider></ControlTransportProvider></MemoryRouter>);

    await user.click(await screen.findByText('新对话设置'));
    const summary = await screen.findByText('查看伙伴设定说明');
    const details = summary.closest('details');
    if (!details) throw new Error('伙伴设定 audit 缺少 details');

    await user.click(summary);
    const source = await screen.findByText('内置伙伴目录');
    expect(summary.closest('summary')).toHaveAttribute('aria-expanded', 'true');
    await user.click(summary);
    expect(summary.closest('summary')).toHaveAttribute('aria-expanded', 'false');
    expect(source).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText('内置伙伴目录')).not.toBeInTheDocument();
      expect(details).not.toHaveAttribute('open');
    });
  });

  it('stacks new-conversation defaults in a narrow PAW window', () => {
    const css = readFileSync('src/features/roles/roles.css', 'utf8');
    expect(css).toMatch(/@container paw-window \(max-width: 620px\)[\s\S]*?\.role-runtime-defaults label\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)/s);
  });

  it('copies the selected built-in partner without changing her lifecycle phase', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: {
      'agent.roles.list': { ok: true, items: previewPersonas },
    } });
    render(<MemoryRouter><ControlTransportProvider transport={transport}><TooltipProvider><RolesFeature /></TooltipProvider></ControlTransportProvider></MemoryRouter>);

    const copyPartner = await screen.findByRole('button', { name: '复制为我的伙伴' });
    await user.click(copyPartner);

    expect(screen.getByRole('dialog', { name: '添加伙伴' })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /构筑阶段/ })).toBeChecked();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(copyPartner).toHaveFocus());
  });

  it('creates a Session with the selected Persona and navigates to it', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: {
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.subagents.templates': { ok: true, items: previewTemplates },
      'agent.sessions.create': { ok: true, session: { id: 'session-past' } },
    } });
    render(
      <MemoryRouter initialEntries={['/roles']}>
        <ControlTransportProvider transport={transport}>
          <TooltipProvider><RolesFeature /><LocationProbe /></TooltipProvider>
        </ControlTransportProvider>
      </MemoryRouter>,
    );

    await user.click(await screen.findByRole('button', { name: /Agent 2/ }));
    await user.click(screen.getByRole('button', { name: '开始对话' }));

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/agent?session=session-past'));
    const create = transport.requests.find((call) => call.request.pathId === 'agent.sessions.create');
    expect(create?.request.body).toEqual({
      title: 'Agent 2 对话',
      mode: 'assistant',
      roleId: 'companion-firstlight-v1',
      roleVersion: '1',
      workspaceRoots: [],
    });
  });

  it('applies reviewed Role Book proposals directly through the preview-bound backend contract', async () => {
    const user = userEvent.setup();
    const persona = previewPersonas[0]!;
    const roleBook = {
      ok: true,
      active: {
        revisionId: 'revision:1',
        revisionNumber: 1,
        status: 'active',
        sections: {
          personality: [],
          capabilities: [],
          recentWork: [{ text: '完成上下文重构' }],
          lessonsAndLimits: [],
          activeCommitments: [],
        },
      },
      history: [],
      dailyDrafts: [{
        draftId: 'role-book-draft:1',
        createdAtMs: 1_800_000_000_000,
        traitProposals: [{ text: '沟通时先给出具体例子', confidence: 0.9, sourceEvidenceIds: ['evidence:1'] }],
        capabilityProposals: [{ text: '能修复 SQLite 事务恢复问题', confidence: 0.95, sourceEvidenceIds: ['evidence:1'] }],
        lessonProposals: [{ text: '工具返回缺失字段时先验证边界契约', confidence: 0.88, sourceEvidenceIds: ['evidence:1'] }],
        commitmentProposals: [{ text: '下一轮发布前完成端到端回归', confidence: 0.92, sourceEvidenceIds: ['evidence:1'] }],
        decision: null,
      }],
    };
    const transport = new MockControlTransport({ routes: {
      'agent.roles.list': { ok: true, items: [persona] },
      'agent.subagents.templates': { ok: true, items: [] },
      'agent.roleBook.get': roleBook,
      'agent.roleBook.activation.preview': {
        ok: true,
        previewToken: 'preview-role-book',
        payloadSha256: 'sha256:role-book',
        summary: {
          risk: 'R1',
          evidenceCount: 1,
          items: ['采用 1 条协作特征', '采用 1 条能力证据'],
          diff: {
            sections: [{
              section: 'personality',
              label: '协作特征',
              added: [{ itemId: 'trait:1', text: '沟通时先给出具体例子', evidenceIds: ['evidence:1'] }],
              removed: [],
              changed: [],
            }],
          },
        },
      },
      'agent.roleBook.activation.apply': {
        ok: true,
        receiptId: 'receipt-role-book',
        rollbackAvailable: true,
        rollbackToken: 'rollback-role-book',
        payloadSha256: 'sha256:role-book',
        result: { revision: { revisionId: 'revision:2', status: 'active' } },
      },
    } });
    render(<MemoryRouter><ControlTransportProvider transport={transport}><TooltipProvider><RolesFeature /></TooltipProvider></ControlTransportProvider></MemoryRouter>);

    await screen.findByText(persona.tagline);
    await user.click(screen.getByRole('button', { name: '她记住的成长' }));
    const lesson = await screen.findByRole('checkbox', { name: /工具返回缺失字段时先验证边界契约/ });
    const applyButton = screen.getByRole('button', { name: '采用所选内容' });
    expect(applyButton).toBeDisabled();
    expect(screen.getByRole('group', { name: '经验与边界' })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: '当前承诺' })).toBeInTheDocument();
    await user.click(lesson);
    expect(applyButton).toBeEnabled();
    await user.click(lesson);
    expect(applyButton).toBeDisabled();
    await user.click(screen.getByRole('checkbox', { name: /沟通时先给出具体例子/ }));
    await user.click(screen.getByRole('checkbox', { name: /能修复 SQLite 事务恢复问题/ }));
    await user.click(lesson);
    await user.click(screen.getByRole('checkbox', { name: /下一轮发布前完成端到端回归/ }));
    await user.click(applyButton);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.roleBook.activation.apply')).toBe(true));
    expect(transport.requests.find((call) => call.request.pathId === 'agent.roleBook.activation.preview')?.request.body).toEqual({
      roleId: persona.roleId,
      roleVersion: persona.version,
      revisionId: '',
      draftId: 'role-book-draft:1',
      traitIndexes: [0],
      capabilityIndexes: [0],
      lessonIndexes: [0],
      commitmentIndexes: [0],
    });
    expect(transport.requests.find((call) => call.request.pathId === 'agent.roleBook.activation.apply')?.request.body).toMatchObject({
      traitIndexes: [0],
      capabilityIndexes: [0],
      lessonIndexes: [0],
      commitmentIndexes: [0],
      previewToken: 'preview-role-book',
      payloadSha256: 'sha256:role-book',
      confirmText: 'apply',
    });
    expect(await screen.findByRole('button', { name: '撤销本次采用' })).toBeInTheDocument();
  });

  it('explains and dismisses an empty Role Book draft without opening a fake diff', async () => {
    const user = userEvent.setup();
    const persona = previewPersonas[0]!;
    const transport = new MockControlTransport({ routes: {
      'agent.roles.list': { ok: true, items: [persona] },
      'agent.roleBook.get': {
        ok: true,
        active: null,
        history: [],
        dailyDrafts: [{
          draftId: 'role-book-draft:empty',
          createdAtMs: 1_800_000_000_000,
          traitProposals: [],
          capabilityProposals: [],
          lessonProposals: [],
          commitmentProposals: [],
          proposalDiagnostics: {
            status: 'no_eligible_evidence',
            provider: 'openai-codex',
            inputChars: 0,
            acceptedProposalCount: 0,
            rejectedProposalCount: 0,
          },
          decision: null,
        }],
      },
      'agent.roleBook.draft.decision': { ok: true },
    } });
    render(<MemoryRouter><ControlTransportProvider transport={transport}><TooltipProvider><RolesFeature /></TooltipProvider></ControlTransportProvider></MemoryRouter>);

    await screen.findByText(persona.tagline);
    await user.click(screen.getByRole('button', { name: '她记住的成长' }));
    expect(await screen.findByText(/没有符合长期成长档案条件的变化/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '采用所选内容' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '忽略' }));

    await waitFor(() => expect(transport.requests).toContainEqual(expect.objectContaining({
      request: expect.objectContaining({
        pathId: 'agent.roleBook.draft.decision',
        body: {
          roleId: persona.roleId,
          roleVersion: persona.version,
          draftId: 'role-book-draft:empty',
          decision: 'rejected',
        },
      }),
    })));
    expect(transport.requests.some((call) => (
      call.request.pathId === 'agent.roleBook.activation.preview'
    ))).toBe(false);
  });

  it('lets builtin partners choose a reasoning model and non-off thinking level', async () => {
    const user = userEvent.setup();
    const configured = {
      ...previewPersonas[0]!,
      defaults: {
        ...previewPersonas[0]!.defaults,
        modelProfile: 'gpt/gpt-5.6-sol',
        thinkingLevel: 'max' as const,
      },
    };
    const updated = {
      ...configured,
      defaults: {
        ...configured.defaults,
        modelProfile: 'openai-codex/gpt-5.6-terra',
        thinkingLevel: 'high' as const,
      },
    };
    const transport = new MockControlTransport({ routes: {
      'agent.roles.list': { ok: true, items: [configured] },
      'agent.subagents.templates': { ok: true, items: [] },
      'agent.role.runtimeDefaults.update': { ok: true, role: updated, defaults: updated.defaults },
      'agent.role.models': {
        ok: true,
        providers: [
          {
            id: 'gpt',
            displayName: 'OpenAI API',
            models: [
              { provider: 'gpt', id: 'gpt-5.6-terra', name: 'GPT-5.6 Terra', api: 'responses', reasoning: true, thinkingLevels: ['off', 'low', 'high'], supportsImages: true, contextWindow: 1000000, maxTokens: 128000 },
              { provider: 'gpt', id: 'gpt-5.6-sol', name: 'GPT-5.6 Sol', api: 'responses', reasoning: true, thinkingLevels: ['off', 'max'], supportsImages: true, contextWindow: 1000000, maxTokens: 128000 },
              { provider: 'deepseek', id: 'deepseek-v4-flash', name: 'DeepSeek V4 Flash', api: 'responses', reasoning: false, thinkingLevels: ['off'], supportsImages: false, contextWindow: 1000000, maxTokens: 128000 },
            ],
          },
          {
            id: 'openai-codex',
            displayName: 'OpenAI Codex',
            models: [
              { provider: 'openai-codex', id: 'gpt-5.6-terra', name: 'GPT-5.6 Terra', api: 'responses', reasoning: true, thinkingLevels: ['off', 'low', 'high'], supportsImages: true, contextWindow: 1000000, maxTokens: 128000 },
            ],
          },
        ],
      },
    } });
    render(<MemoryRouter><ControlTransportProvider transport={transport}><TooltipProvider><RolesFeature /></TooltipProvider></ControlTransportProvider></MemoryRouter>);

    expect(await screen.findByText('内置伙伴 · 复制后可以调整')).toBeInTheDocument();
    await user.click(screen.getByText('新对话设置'));
    expect((await screen.findAllByText('GPT-5.6 Sol · OpenAI API')).length).toBeGreaterThan(0);
    await user.click(screen.getByLabelText('角色默认模型'));
    expect(await screen.findByRole('option', { name: 'GPT-5.6 Terra · OpenAI API' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'GPT-5.6 Terra · OpenAI Codex' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'DeepSeek V4 Flash' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('option', { name: 'GPT-5.6 Terra · OpenAI Codex' }));
    await user.click(screen.getByLabelText('角色默认推理强度'));
    expect(screen.queryByRole('option', { name: '不启用推理' })).not.toBeInTheDocument();
    await user.click(await screen.findByRole('option', { name: '高' }));
    await user.click(screen.getByRole('button', { name: '保存默认设置' }));
    await waitFor(() => expect(transport.requests.find((call) => call.request.pathId === 'agent.role.runtimeDefaults.update')?.request.body).toEqual({
      roleId: configured.roleId,
      roleVersion: configured.version,
      provider: 'openai-codex',
      modelId: 'gpt-5.6-terra',
      thinkingLevel: 'high',
    }));
  });

  it('exposes Flash as a selectable fast-scan role with explicit boundaries', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: {
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.subagents.templates': { ok: true, items: previewTemplates },
      'agent.role.models': {
        ok: true,
        providers: [{
          id: 'gpt',
          displayName: 'GPT',
          models: [{ provider: 'gpt', id: 'gpt-5.6-luna', name: 'GPT-5.6 Luna', api: 'responses', reasoning: true, thinkingLevels: ['off', 'low', 'max'], supportsImages: true, contextWindow: 1000000, maxTokens: 128000 }],
        }],
      },
    } });
    render(<MemoryRouter><ControlTransportProvider transport={transport}><TooltipProvider><RolesFeature /></TooltipProvider></ControlTransportProvider></MemoryRouter>);

    await user.click(await screen.findByRole('button', { name: /Agent 4/ }));
    expect(screen.getByText('超长材料高速扫读与提取')).toBeInTheDocument();
    expect(screen.getByText('归类、去重和格式转换')).toBeInTheDocument();
    expect(screen.getByText('复杂推理')).toBeInTheDocument();
    expect(screen.getByText('最终验收')).toBeInTheDocument();
    await user.click(screen.getByText('新对话设置'));
    expect(screen.getAllByText('GPT-5.6 Luna · GPT').length).toBeGreaterThan(0);
    expect(screen.getByLabelText('角色默认推理强度')).toHaveTextContent('低');
  });

  it('does not substitute preview Personas when the native catalog is empty', async () => {
    const transport = new StubControlTransport('native', {
      'agent.roles.list': { ok: true, items: [] },
      'agent.subagents.templates': { ok: true, items: [] },
    });
    render(<MemoryRouter><ControlTransportProvider transport={transport}><TooltipProvider><RolesFeature /></TooltipProvider></ControlTransportProvider></MemoryRouter>);

    expect(await screen.findByText('还没有伙伴')).toBeInTheDocument();
    expect(screen.getByText('添加后，可以为不同类型的工作选择更合适的陪伴方式。')).toBeInTheDocument();
    expect(screen.queryByText('此刻陪你输入，也陪你把事情想清楚')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '开始对话' })).not.toBeInTheDocument();
  });

  it('does not make the partner catalog depend on task-template availability', async () => {
    const user = userEvent.setup();
    const transport = new StubControlTransport('native', {
      'agent.roles.list': { ok: true, items: previewPersonas },
    });
    render(<MemoryRouter><ControlTransportProvider transport={transport}><TooltipProvider><RolesFeature /></TooltipProvider></ControlTransportProvider></MemoryRouter>);

    expect(await screen.findByText(previewPersonas[0]!.tagline)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '开始对话' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: /Agent 2/ }));
    expect(transport.requests.map((request) => request.pathId)).toEqual([
      'agent.roles.list',
      'agent.role.models',
      'agent.configuration.get',
    ]);
  });

  it('keeps a pending role catalog distinct from a genuinely empty catalog', async () => {
    const roles = deferred<unknown>();
    const transport = new StubControlTransport('native', {
      'agent.roles.list': () => roles.promise,
      'agent.role.models': { ok: true, providers: [] },
      'agent.configuration.get': { ok: true },
    });
    render(<MemoryRouter><ControlTransportProvider transport={transport}><TooltipProvider><RolesFeature /></TooltipProvider></ControlTransportProvider></MemoryRouter>);

    expect(screen.getByText('正在读取伙伴')).toBeInTheDocument();
    expect(screen.queryByText('还没有伙伴')).not.toBeInTheDocument();
    await act(async () => roles.resolve({ ok: true, items: [] }));
    expect(await screen.findByText('还没有伙伴')).toBeInTheDocument();
  });

  it('makes a role catalog failure recoverable without inventing fallback partners', async () => {
    const user = userEvent.setup();
    let attempts = 0;
    const transport = new StubControlTransport('native', {
      'agent.roles.list': () => {
        attempts += 1;
        if (attempts === 1) throw new Error('role service unavailable secret=abc');
        return { ok: true, items: previewPersonas };
      },
      'agent.role.models': { ok: true, providers: [] },
      'agent.configuration.get': { ok: true },
    });
    render(<MemoryRouter><ControlTransportProvider transport={transport}><TooltipProvider><RolesFeature /></TooltipProvider></ControlTransportProvider></MemoryRouter>);

    expect(await screen.findByRole('alert')).toHaveTextContent('伙伴目录没有打开');
    expect(screen.queryByText(/secret=abc/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '开始对话' })).not.toBeInTheDocument();
    expect(screen.queryByText('还没有伙伴')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '重新读取伙伴' }));
    expect(await screen.findByText(previewPersonas[0]!.tagline)).toBeInTheDocument();
    expect(attempts).toBe(2);
  });

  it('creates a user-defined role through the real role creation contract', async () => {
    const user = userEvent.setup();
    const createdRole = {
      ...previewPersonas[2],
      roleId: 'morning-guide-v1',
      displayName: '澄·晨光',
      tagline: '先看清今天，再稳稳向前',
      summary: '适合陪我整理早晨计划与关键证据。',
      traits: ['清晰', '温和'] as ['清晰', '温和'],
      defaults: {
        ...previewPersonas[2]!.defaults,
        modelPolicy: 'session-selected',
      },
      visualProfile: previewPersonas[0]!.visualProfile,
      selectableModes: ['assistant', 'coordinator'] as ['assistant', 'coordinator'],
      runtimeCharacteristics: {
        ...previewPersonas[2]!.runtimeCharacteristics,
        suitableTasks: ['整理早晨计划', '核对关键证据'] as ['整理早晨计划', '核对关键证据'],
        unsuitableTasks: ['替用户做高风险决定'] as ['替用户做高风险决定'],
      },
    };
    const transport = new MockControlTransport({ routes: {
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.subagents.templates': { ok: true, items: previewTemplates },
      'agent.roles.create': { ok: true, role: createdRole },
      'agent.sessions.create': { ok: true, session: { id: 'session-morning-guide' } },
    } });
    render(
      <MemoryRouter initialEntries={['/roles']}>
        <ControlTransportProvider transport={transport}>
          <TooltipProvider><RolesFeature /><LocationProbe /></TooltipProvider>
        </ControlTransportProvider>
      </MemoryRouter>,
    );

    await user.click(await screen.findByRole('button', { name: '添加伙伴' }));
    const name = screen.getByRole('textbox', { name: '伙伴名字' });
    const tagline = screen.getByRole('textbox', { name: '角色一句话介绍' });
    const summary = screen.getByRole('textbox', { name: '伙伴说明' });
    const suitableTasks = screen.getByRole('textbox', { name: '适合任务' });
    const unsuitableTasks = screen.getByRole('textbox', { name: '不建议任务' });
    expect(name).toHaveAttribute('maxlength', '40');
    expect(tagline).toHaveAttribute('maxlength', '80');
    expect(summary).toHaveAttribute('maxlength', '180');
    await user.type(name, '澄·晨光');
    await user.type(tagline, '先看清今天，再稳稳向前');
    await user.type(summary, '适合陪我整理早晨计划与关键证据。');
    await user.type(suitableTasks, '整理早晨计划{Enter}核对关键证据');
    await user.type(unsuitableTasks, '替用户做高风险决定');
    expect(screen.getByText(/模型和工具在运行设置中独立管理/)).toBeInTheDocument();
    await user.click(screen.getByRole('radio', { name: /构筑阶段/ }));
    const trait = screen.getByRole('textbox', { name: '新增表达特征' });
    await user.type(trait, '清晰{Enter}');
    await user.type(trait, '温和');
    await user.click(screen.getByRole('checkbox', { name: '参与多人协作' }));
    await user.click(screen.getByRole('button', { name: '添加伙伴' }));

    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.roles.create')).toBe(true));
    expect(transport.requests.find((call) => call.request.pathId === 'agent.roles.create')?.request.body).toEqual({
      displayName: '澄·晨光',
      tagline: '先看清今天，再稳稳向前',
      summary: '适合陪我整理早晨计划与关键证据。',
      traits: ['清晰', '温和'],
      timelineModel: 'sol',
      selectableModes: ['assistant', 'coordinator'],
      suitableTasks: ['整理早晨计划', '核对关键证据'],
      unsuitableTasks: ['替用户做高风险决定'],
    });
    expect(await screen.findByRole('button', { name: /澄·晨光/ })).toHaveAttribute('aria-current', 'true');
    expect(screen.getAllByText('构筑阶段')).not.toHaveLength(0);
    expect(screen.getAllByText('我的伙伴').length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: '编辑伙伴' })).toBeInTheDocument();
    expect(screen.getAllByText('澄·晨光').length).toBeGreaterThan(0);
    expect(screen.queryByRole('dialog', { name: '添加伙伴' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '开始对话' }));
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/agent?session=session-morning-guide'));
    expect(transport.requests.find((call) => call.request.pathId === 'agent.sessions.create')?.request.body).toEqual({
      title: '澄·晨光 对话',
      mode: 'assistant',
      roleId: 'morning-guide-v1',
      roleVersion: '1',
      workspaceRoots: [],
    });
  });

  it('edits a user-owned companion while builtin companions stay read-only', async () => {
    const user = userEvent.setup();
    const customRole = {
      ...previewPersonas[2]!,
      roleId: 'persona-rain-v1',
      displayName: '澄·雨天',
      tagline: '陪你安静整理',
      summary: '偏向温和复盘。',
      traits: ['温和'] as ['温和'],
      defaults: { ...previewPersonas[2]!.defaults, modelPolicy: 'session-selected' },
      selectableModes: ['assistant'] as ['assistant'],
      runtimeCharacteristics: {
        ...previewPersonas[2]!.runtimeCharacteristics,
        suitableTasks: ['温和复盘'] as ['温和复盘'],
        unsuitableTasks: ['替用户做高风险决定'] as ['替用户做高风险决定'],
      },
    };
    const updatedRole = {
      ...customRole,
      displayName: '澄·暮雨',
      tagline: '先安静看清，再一起往前',
      summary: '偏向温和复盘与明确下一步。',
      traits: ['温和', '清楚'] as ['温和', '清楚'],
      selectableModes: ['assistant', 'coordinator'] as ['assistant', 'coordinator'],
      runtimeCharacteristics: {
        ...customRole.runtimeCharacteristics,
        suitableTasks: ['温和复盘', '明确下一步'] as ['温和复盘', '明确下一步'],
        unsuitableTasks: ['未经证据的个人判断'] as ['未经证据的个人判断'],
      },
    };
    const transport = new MockControlTransport({ routes: {
      'agent.roles.list': { ok: true, items: [previewPersonas[0], customRole] },
      'agent.subagents.templates': { ok: true, items: [] },
      'agent.roles.update': { ok: true, role: updatedRole },
    } });
    render(<MemoryRouter><ControlTransportProvider transport={transport}><TooltipProvider><RolesFeature /></TooltipProvider></ControlTransportProvider></MemoryRouter>);

    expect(await screen.findByRole('button', { name: '复制为我的伙伴' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '编辑伙伴' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /澄·雨天/ }));
    await user.click(screen.getByRole('button', { name: '编辑伙伴' }));
    expect(screen.getByRole('dialog', { name: '编辑伙伴' })).toHaveTextContent('工具与安全边界仍由系统管理');
    await user.clear(screen.getByRole('textbox', { name: '伙伴名字' }));
    await user.type(screen.getByRole('textbox', { name: '伙伴名字' }), '澄·暮雨');
    await user.clear(screen.getByRole('textbox', { name: '角色一句话介绍' }));
    await user.type(screen.getByRole('textbox', { name: '角色一句话介绍' }), '先安静看清，再一起往前');
    await user.clear(screen.getByRole('textbox', { name: '伙伴说明' }));
    await user.type(screen.getByRole('textbox', { name: '伙伴说明' }), '偏向温和复盘与明确下一步。');
    await user.clear(screen.getByRole('textbox', { name: '适合任务' }));
    await user.type(screen.getByRole('textbox', { name: '适合任务' }), '温和复盘{Enter}明确下一步');
    await user.clear(screen.getByRole('textbox', { name: '不建议任务' }));
    await user.type(screen.getByRole('textbox', { name: '不建议任务' }), '未经证据的个人判断');
    await user.type(screen.getByRole('textbox', { name: '新增表达特征' }), '清楚{Enter}');
    await user.click(screen.getByRole('checkbox', { name: '参与多人协作' }));
    await user.click(screen.getByRole('button', { name: '保存伙伴' }));

    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'agent.roles.update')).toBe(true));
    expect(transport.requests.find((call) => call.request.pathId === 'agent.roles.update')?.request.body).toEqual({
      roleId: customRole.roleId,
      roleVersion: customRole.version,
      displayName: '澄·暮雨',
      tagline: '先安静看清，再一起往前',
      summary: '偏向温和复盘与明确下一步。',
      traits: ['温和', '清楚'],
      timelineModel: 'luna',
      selectableModes: ['assistant', 'coordinator'],
      suitableTasks: ['温和复盘', '明确下一步'],
      unsuitableTasks: ['未经证据的个人判断'],
    });
    expect(await screen.findByRole('button', { name: /澄·暮雨/ })).toHaveAttribute('aria-current', 'true');
    expect(screen.getByRole('button', { name: '编辑伙伴' })).toBeInTheDocument();
  });

  it('sets a real default companion and protects it from accidental removal', async () => {
    const user = userEvent.setup();
    const customRole = {
      ...previewPersonas[2]!,
      roleId: 'persona-default-v1',
      displayName: '澄·灯塔',
      tagline: '每次新对话都先帮你找到方向',
      defaults: { ...previewPersonas[2]!.defaults, modelPolicy: 'session-selected' },
    };
    const configuration = (revision: number, roleId: string) => ({
      ok: true,
      configuration: {
        revision,
        configuration: {
          sessionDefaults: { roleId, roleVersion: '1' },
        },
      },
    });
    const transport = new MockControlTransport({ routes: {
      'agent.roles.list': { ok: true, items: [previewPersonas[0], customRole] },
      'agent.subagents.templates': { ok: true, items: [] },
      'agent.configuration.get': configuration(7, previewPersonas[0]!.roleId),
      'agent.configuration.update': configuration(8, customRole.roleId),
    } });
    render(<MemoryRouter><ControlTransportProvider transport={transport}><TooltipProvider><RolesFeature /></TooltipProvider></ControlTransportProvider></MemoryRouter>);

    await user.click(await screen.findByRole('button', { name: /澄·灯塔/ }));
    await user.click(screen.getByRole('button', { name: '设为默认' }));

    await waitFor(() => expect(screen.getByText('新对话默认伙伴')).toBeInTheDocument());
    expect(transport.requests.find((call) => call.request.pathId === 'agent.configuration.update')?.request.body).toEqual({
      expectedRevision: 7,
      changes: {
        'sessionDefaults.roleId': customRole.roleId,
        'sessionDefaults.roleVersion': customRole.version,
      },
      updatedBy: 'roles-ui',
    });
    expect(screen.getByRole('button', { name: '移除伙伴' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '移除伙伴' })).toHaveAttribute('title', '请先选择另一位默认伙伴');
  });

  it('removes a user companion from new selection without implying old conversations are deleted', async () => {
    const user = userEvent.setup();
    const customRole = {
      ...previewPersonas[2]!,
      roleId: 'persona-archive-v1',
      displayName: '澄·旧页',
      tagline: '陪你整理已经完成的章节',
      defaults: { ...previewPersonas[2]!.defaults, modelPolicy: 'session-selected' },
    };
    const transport = new MockControlTransport({ routes: {
      'agent.roles.list': { ok: true, items: [previewPersonas[0], customRole] },
      'agent.subagents.templates': { ok: true, items: [] },
      'agent.configuration.get': {
        ok: true,
        configuration: {
          revision: 3,
          configuration: {
            sessionDefaults: { roleId: previewPersonas[0]!.roleId, roleVersion: '1' },
          },
        },
      },
      'agent.roles.archive': { ok: true, roleId: customRole.roleId, roleVersion: customRole.version },
    } });
    render(<MemoryRouter><ControlTransportProvider transport={transport}><TooltipProvider><RolesFeature /></TooltipProvider></ControlTransportProvider></MemoryRouter>);

    await user.click(await screen.findByRole('button', { name: /澄·旧页/ }));
    await user.click(screen.getByRole('button', { name: '移除伙伴' }));
    const dialog = screen.getByRole('dialog', { name: '移除这个伙伴？' });
    expect(dialog).toHaveTextContent('已有对话仍保留并可继续');
    await user.click(within(dialog).getByRole('button', { name: '移除伙伴' }));

    await waitFor(() => expect(screen.queryByRole('button', { name: /澄·旧页/ })).not.toBeInTheDocument());
    expect(transport.requests.find((call) => call.request.pathId === 'agent.roles.archive')?.request.body).toEqual({
      roleId: customRole.roleId,
      roleVersion: customRole.version,
    });
    expect(screen.getByRole('status')).toHaveTextContent('已有对话仍可继续');
  });

  it('keeps a role creation failure visible inside the dialog without losing the draft', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: {
      'agent.roles.list': { ok: true, items: previewPersonas },
      'agent.subagents.templates': { ok: true, items: previewTemplates },
      'agent.roles.create': () => { throw new Error('角色名称已存在'); },
    } });
    render(<MemoryRouter><ControlTransportProvider transport={transport}><TooltipProvider><RolesFeature /></TooltipProvider></ControlTransportProvider></MemoryRouter>);

    await user.click(await screen.findByRole('button', { name: '添加伙伴' }));
    await user.type(screen.getByRole('textbox', { name: '伙伴名字' }), '澄·晨光');
    await user.type(screen.getByRole('textbox', { name: '角色一句话介绍' }), '先看清今天');
    await user.type(screen.getByRole('textbox', { name: '伙伴说明' }), '陪我整理今天的重点。');
    await user.type(screen.getByRole('textbox', { name: '适合任务' }), '整理今天重点');
    await user.type(screen.getByRole('textbox', { name: '不建议任务' }), '高风险独立决定');
    await user.type(screen.getByRole('textbox', { name: '新增表达特征' }), '清晰');
    await user.click(screen.getByRole('button', { name: '添加伙伴' }));

    const dialog = screen.getByRole('dialog', { name: '添加伙伴' });
    expect(await screen.findByRole('alert')).toHaveTextContent('角色名称已存在');
    expect(dialog).toContainElement(screen.getByRole('alert'));
    expect(screen.getByRole('textbox', { name: '伙伴名字' })).toHaveValue('澄·晨光');
  });
});

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}{location.search}</output>;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function modelRoutingConfiguration(
  revision: number,
  toolAgent: { modelProfile: string; thinkingLevel: string } = {
    modelProfile: 'inherit',
    thinkingLevel: 'inherit',
  },
) {
  return {
    ok: true,
    configuration: {
      revision,
      configuration: {
        sessionDefaults: {
          roleId: previewPersonas[0]!.roleId,
          roleVersion: previewPersonas[0]!.version,
        },
        modelRouting: {
          primary: { modelProfile: 'inherit', thinkingLevel: 'inherit' },
          traceDiagnostic: { modelProfile: 'inherit', thinkingLevel: 'inherit' },
          toolAgent,
          subagent: { modelProfile: 'inherit', thinkingLevel: 'inherit' },
          roomCoordinator: { modelProfile: 'inherit', thinkingLevel: 'inherit' },
        },
      },
    },
  };
}
