import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { MotionProvider } from '@/design/motion';
import { PawOsAppearanceProvider } from '@/design/paw-os-themes';
import { ThemeProvider } from '@/design/themes';
import { PawOsAppSurfaceProvider, PawOsDesktopProvider } from '@/features/paw-os/surface-context';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';

vi.mock('@/features/approvals', () => ({ ApprovalsFeature: () => <h1>审批真实界面</h1> }));
vi.mock('@/features/configuration', () => ({ ConfigurationFeature: () => <h1>配置真实界面</h1> }));
vi.mock('@/features/context-debug', () => ({ ContextDebugFeature: () => <h1>上下文真实界面</h1> }));
vi.mock('@/features/diagnostics', () => ({ DiagnosticsFeature: () => <h1>诊断真实界面</h1> }));
vi.mock('@/features/governance', () => ({ GovernanceFeature: () => <h1>治理真实界面</h1> }));
vi.mock('@/features/history', () => ({ HistoryFeature: () => <h1>输入记录真实界面</h1> }));
vi.mock('@/features/input-method', () => ({
  InputLexiconFeature: () => <h1>词库真实界面</h1>,
  InputMethodFeature: () => <h1>输入法真实界面</h1>,
}));
vi.mock('@/features/observability', () => ({ ObservabilityFeature: () => <h1>活动真实界面</h1> }));
vi.mock('@/features/plugins', () => ({ PluginsFeature: () => <h1>Package 生命周期真实界面</h1> }));
vi.mock('@/features/team/TeamSharedResources', () => ({
  TeamSharedResources: ({ pageId }: { pageId: string }) => <h1>团队共享资源真实界面 {pageId}</h1>,
}));
vi.mock('@/features/voice', () => ({ VoiceFeature: () => <h1>语音真实界面</h1> }));

import {
  PawSystemAppsMigrated,
  type PawSystemAppId,
} from './PawSystemAppsMigrated';

afterEach(() => {
  cleanup();
  document.querySelector('meta[name="paw-deployment"]')?.remove();
  window.localStorage.clear();
  delete document.documentElement.dataset.theme;
  document.documentElement.style.removeProperty('color-scheme');
});

describe('PawSystemAppsMigrated', () => {
  it.each([
    ['input-studio', '/history', '输入记录'],
    ['app-center', '/plugins?view=proposals', '建议'],
    ['app-center', '/plugins?view=capabilities&sessionId=example', '功能开关'],
    ['system-monitor', '/diagnostics', '诊断'],
    ['system-settings', '/approvals', '审批'],
  ] as const)('keeps %s route %s selected in the migrated App navigation', (appId, route, label) => {
    renderSystemApp(appId, route);

    const navigation = screen.getByRole('navigation', { name: new RegExp('页面$') });
    expect(within(navigation).getByRole('button', { name: label })).toHaveAttribute('aria-current', 'page');
  });

  it('uses the Team resource surface and hides local installation controls in Team App Center', async () => {
    const teamMeta = document.createElement('meta');
    teamMeta.name = 'paw-deployment';
    teamMeta.content = 'team';
    document.head.appendChild(teamMeta);
    const transport = baseTransport({
      'agent.extensions.proposals': {
        ok: true,
        items: [{ proposalId: 'local-proposal', summary: { action: 'install' } }],
      },
    });
    const user = userEvent.setup();
    renderSystemApp('app-center', '/plugins', transport);

    expect(await screen.findByRole('heading', { name: '团队共享资源真实界面 installed' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '已发布版本' })).toHaveAttribute('aria-current', 'page');
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.extensions.proposals')).toBe(false);

    await user.click(screen.getByRole('button', { name: '发布目录' }));
    expect(await screen.findByRole('heading', { name: '团队共享资源真实界面 catalog' })).toBeInTheDocument();
  });

  it('opens the optimization report as an external webpage instead of mounting it in System Monitor', async () => {
    const user = userEvent.setup();
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    renderSystemApp('system-monitor', '/observability');

    const link = screen.getByRole('button', { name: '优化报告（网页）' });
    expect(link).not.toHaveAttribute('aria-current');
    expect(screen.queryByRole('heading', { name: '优化报告真实界面' })).not.toBeInTheDocument();

    await user.click(link);

    expect(open).toHaveBeenCalledWith(
      expect.stringMatching(/\/evolution-report$/),
      '_blank',
      'noopener,noreferrer',
    );
  });

  it('leaves App identity to the shared window chrome instead of repeating it in the rail', () => {
    renderSystemApp('input-studio', '/input');

    const rail = document.querySelector('.paw-system-app__nav');
    expect(rail?.querySelector('.paw-app-icon')).toBeNull();
    expect(rail?.firstElementChild).toHaveAttribute('aria-controls', screen.getByRole('navigation').id);
    expect(screen.getByRole('navigation', { name: 'Input Studio页面' })).toBeInTheDocument();
  });

  it.each(['input-studio', 'app-center', 'system-monitor', 'system-settings'] as const)('collapses %s navigation and preserves the current workspace', async (appId) => {
    const user = userEvent.setup();
    renderSystemApp(appId, '');
    const toggle = screen.getByRole('button', { name: /收起.*导航$/ });
    const navigation = screen.getByRole('navigation');
    const workspace = document.querySelector('.paw-system-app__workspace');
    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(navigation).not.toBeVisible();
    expect(document.querySelector('.paw-system-app__workspace')).toBe(workspace);
    await user.click(toggle);
    expect(navigation).toBeVisible();
  });

  it('names the current page with its group in the stage chrome strip', () => {
    renderSystemApp('system-settings', '/appearance');

    const title = document.querySelector('.paw-system-app__page-title');
    expect(title?.textContent?.trim()).toBe('通用 · 外观');
  });

  it('builds the App as a rail beside one chrome band and one workspace, not a page', () => {
    renderSystemApp('system-monitor', '/observability');

    const app = document.querySelector('.paw-system-app') as HTMLElement;
    const frame = app.querySelector(':scope > .paw-system-app__frame') as HTMLElement;
    expect(frame).toBeInTheDocument();
    // The rail width answers a container query on the frame, so the frame —
    // never the App itself — has to be the element carrying the two columns.
    expect([...frame.children].map((node) => node.className)).toEqual([
      'paw-system-app__nav',
      'paw-system-app__stage',
    ]);

    const stage = frame.querySelector('.paw-system-app__stage') as HTMLElement;
    expect([...stage.children].map((node) => node.className)).toEqual([
      'paw-system-app__chrome',
      'paw-system-app__workspace',
    ]);
    // One scrolling band absorbs resize; the page mounts inside it.
    expect(stage.querySelector('.paw-system-app__workspace > .paw-system-app__page')).toBeInTheDocument();
  });

  it('carries page purpose in the chrome band instead of a hero header above the content', () => {
    renderSystemApp('app-center', '/plugins?view=catalog');

    const purpose = document.querySelector('.paw-system-app__page-purpose');
    expect(purpose?.textContent).toBe('安装之前先看清来源、权限与版本');
    // Decoration for the eye only: the page below owns the accessible copy.
    expect(purpose).toHaveAttribute('aria-hidden', 'true');
    expect(document.querySelector('.paw-system-app__chrome')?.children).toHaveLength(2);
  });

  it.each([
    ['input-studio', 'studio'],
    ['app-center', 'gallery'],
    ['system-monitor', 'instrument'],
    ['system-settings', 'sheet'],
  ] as const)('gives %s a purpose-specific stage pace without a private accent', (appId, stage) => {
    renderSystemApp(appId, '');

    const app = document.querySelector('.paw-system-app') as HTMLElement;
    expect(app).toHaveAttribute('data-stage', stage);
    expect(app).toHaveAttribute('data-system-app', appId);
    expect(app.style.getPropertyValue('--paw-system-accent')).toBe('');
  });

  it('keeps the whole accessible rail name when the collapsed rail hides the label', async () => {
    const transport = baseTransport({
      'agent.approvals.list': { ok: true, items: [previewApprovalStub('approval:one', 'pending')] },
    });
    renderSystemApp('system-settings', '/configuration', transport);

    // The narrow container query hides only `.paw-system-app__nav-label`, so
    // the button has to keep its name and tooltip somewhere else.
    const approvals = await screen.findByRole('button', { name: '审批（1 项待处理）' });
    expect(approvals).toHaveAttribute('title', '审批（1 项待处理）');
    expect(approvals.querySelector('.paw-system-app__nav-label')?.textContent).toBe('审批');
    expect(approvals.querySelector('.paw-system-app__nav-badge')?.textContent).toBe('1');
  });

  it('carves the Settings and Monitor rails into labelled groups', () => {
    renderSystemApp('system-settings', '/configuration');
    expect(groupLabels()).toEqual(['通用', 'Agent', '安全与信任']);

    cleanup();
    renderSystemApp('system-monitor', '/observability');
    expect(groupLabels()).toEqual(['实时', '实验', '排查']);
  });

  it('counts pending approvals on the Settings rail without inventing numbers', async () => {
    const transport = baseTransport({
      'agent.approvals.list': {
        ok: true,
        items: [
          previewApprovalStub('approval:one', 'pending'),
          previewApprovalStub('approval:two', 'pending'),
          previewApprovalStub('approval:done', 'applied'),
        ],
      },
    });
    renderSystemApp('system-settings', '/configuration', transport);

    const approvalsButton = await screen.findByRole('button', { name: '审批（2 项待处理）' });
    const badge = approvalsButton.querySelector('.paw-system-app__nav-badge');
    expect(badge?.textContent).toBe('2');
    expect(badge).toHaveAttribute('data-tone', 'decision');
  });

  it('counts pending install proposals on the App Center rail', async () => {
    const transport = baseTransport({
      'agent.extensions.proposals': {
        ok: true,
        items: [
          { proposalId: 'proposal:one', summary: { action: 'install', pluginId: 'pkg-one' } },
          { proposalId: 'proposal:two', summary: { action: 'update', pluginId: 'pkg-two' } },
        ],
      },
    });
    renderSystemApp('app-center', '/plugins', transport);

    const proposalsButton = await screen.findByRole('button', { name: '建议（2 项待确认）' });
    const badge = proposalsButton.querySelector('.paw-system-app__nav-badge');
    expect(badge?.textContent).toBe('2');
    expect(badge).toHaveAttribute('data-tone', 'decision');
  });

  it('relays components that report a problem on the Monitor rail', async () => {
    const transport = baseTransport({
      'diagnostics.runtime': {
        ok: true,
        components: {
          sidecar: { ok: false, status: 'stopped' },
          predictor: { ok: true, status: 'ready' },
          inputMethod: { ok: false, status: 'unavailable' },
        },
      },
    });
    renderSystemApp('system-monitor', '/observability', transport);

    const diagnosticsButton = await screen.findByRole('button', { name: '诊断（2 项需要检查）' });
    expect(diagnosticsButton.querySelector('.paw-system-app__nav-badge')).toHaveAttribute('data-tone', 'attention');
  });

  it('keeps the Settings rail quiet when the approvals route is unavailable', async () => {
    renderSystemApp('system-settings', '/configuration');

    expect(await screen.findByRole('button', { name: '审批' })).toBeInTheDocument();
    await waitFor(() => expect(document.querySelector('.paw-system-app__nav-badge')).toBeNull());
  });

  it('keeps the Monitor and App Center rails quiet without live evidence', async () => {
    renderSystemApp('system-monitor', '/observability');
    expect(await screen.findByRole('button', { name: '诊断' })).toBeInTheDocument();
    await waitFor(() => expect(document.querySelector('.paw-system-app__nav-badge')).toBeNull());

    cleanup();
    renderSystemApp('app-center', '/plugins');
    expect(await screen.findByRole('button', { name: '建议' })).toBeInTheDocument();
    await waitFor(() => expect(document.querySelector('.paw-system-app__nav-badge')).toBeNull());
  });

  it('moves between Input Studio pages while retaining the real feature owners', async () => {
    const user = userEvent.setup();
    renderSystemApp('input-studio', '/input');

    expect(await screen.findByRole('heading', { name: '输入法真实界面' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '语音' }));
    expect(await screen.findByRole('heading', { name: '语音真实界面' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '语音' })).toHaveAttribute('aria-current', 'page');
  });

  it('gives 输入法 and 词库 independent page owners instead of repeating one long page', async () => {
    const user = userEvent.setup();
    renderSystemApp('input-studio', '/input');

    expect(await screen.findByRole('heading', { name: '输入法真实界面' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '词库真实界面' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '词库' }));

    expect(await screen.findByRole('heading', { name: '词库真实界面' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '输入法真实界面' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '词库' })).toHaveAttribute('aria-current', 'page');
  });

  it('loads the real Agent model catalog without inventing model options', async () => {
    const transport = baseTransport({
      'agent.role.models': {
        ok: true,
        providers: [{
          id: 'openai-codex',
          displayName: 'OpenAI Codex',
          models: [{
            provider: 'openai-codex',
            id: 'gpt-5.6-terra',
            name: 'GPT-5.6 Terra',
            api: 'responses',
            reasoning: true,
            thinkingLevels: ['off', 'medium', 'high'],
            supportsImages: true,
            contextWindow: 128_000,
            maxTokens: 32_000,
          }],
        }],
      },
    });
    renderSystemApp('system-settings', '/configuration?view=agent', transport);

    // Same smooth gesture as the Session composer: real models are rows on the
    // surface under their provider, not entries hidden inside a native field.
    const models = await screen.findByRole('listbox', { name: 'Agent 模型' });
    expect(within(models).getByRole('group', { name: 'openai-codex' })).toBeInTheDocument();
    expect(within(models).getByRole('option', { name: '选择模型 GPT-5.6 Terra' })).toBeInTheDocument();
    expect(within(models).getByRole('option', { name: '选择模型 自动选择' })).toHaveAttribute('aria-selected', 'true');
    expect(within(models).getAllByRole('option')).toHaveLength(2);
    expect(transport.requests.map(({ request }) => request.pathId)).toContain('agent.role.models');
    expect(transport.requests.map(({ request }) => request.pathId)).toContain('configuration.settings');
  });

  it('configures Room planets separately from private satellites through the model-routing owner', async () => {
    const user = userEvent.setup();
    const transport = baseTransport({
      'agent.role.models': {
        ok: true,
        providers: [{
          id: 'openai-codex',
          displayName: 'OpenAI Codex',
          models: [{
            provider: 'openai-codex',
            id: 'gpt-5.6-terra',
            name: 'GPT-5.6 Terra',
            api: 'responses',
            reasoning: true,
            thinkingLevels: ['off', 'medium', 'high'],
            supportsImages: true,
            contextWindow: 128_000,
            maxTokens: 32_000,
          }],
        }],
      },
      'agent.configuration.get': systemModelRoutingConfiguration(21),
      'agent.configuration.update': systemModelRoutingConfiguration(22, {
        roomCoordinator: {
          modelProfile: 'openai-codex/gpt-5.6-terra',
          thinkingLevel: 'high',
        },
      }),
    });
    renderSystemApp('system-settings', '/configuration?view=agent', transport);

    expect(await screen.findByText('配置 #21')).toBeInTheDocument();
    expect(screen.getByLabelText('Room 行星伙伴默认模型')).toBeInTheDocument();
    expect(screen.getByLabelText('私有 Tool Agent默认模型')).toBeInTheDocument();
    expect(screen.getByLabelText('私有调研卫星默认模型')).toBeInTheDocument();

    await user.click(screen.getByLabelText('Room 行星伙伴默认模型'));
    await user.click(await screen.findByRole('option', { name: 'GPT-5.6 Terra · OpenAI Codex' }));
    await user.click(screen.getByLabelText('Room 行星伙伴默认推理强度'));
    await user.click(await screen.findByRole('option', { name: '高' }));
    await user.click(screen.getByRole('button', { name: '保存Room 行星伙伴模型分工' }));

    await waitFor(() => expect(transport.requests.find(
      ({ request }) => request.pathId === 'agent.configuration.update',
    )?.request.body).toEqual({
      expectedRevision: 21,
      changes: {
        'modelRouting.roomCoordinator': {
          modelProfile: 'openai-codex/gpt-5.6-terra',
          thinkingLevel: 'high',
        },
      },
      updatedBy: 'system-agent-settings-ui',
    }));
    expect(await screen.findByText('配置 #22')).toBeInTheDocument();
  });

  it('saves Agent defaults through the governed configuration authority and re-reads them', async () => {
    const user = userEvent.setup();
    let executionMode = 'per_action';
    let settingsReads = 0;
    const payloadSha256 = 'c'.repeat(64);
    const transport = baseTransport({
      'configuration.settings': () => {
        settingsReads += 1;
        return agentPreferenceSettings(executionMode);
      },
      'configuration.settings.preview': {
        schemaVersion: 'rag-ime.management-work-preview.v1',
        ok: true,
        previewToken: 'agent-settings-preview',
        pathId: 'configuration.settings.apply',
        payloadSha256,
        expectedRevision: { runtimeRevision: 7, subjectRevision: 'settings:7' },
        expiresAtMs: Date.now() + 60_000,
        requiredConfirm: 'apply',
        summary: { title: '保存 Agent 默认设置', items: ['更新执行权限'], risk: 'R1' },
      },
      'configuration.settings.apply': (request: ControlRequest) => {
        const changes = (request.body as Record<string, unknown>).changes as Record<string, unknown>;
        executionMode = String(changes['agent.defaults.executionMode']);
        return {
          schemaVersion: 'rag-ime.management-work-receipt.v1',
          ok: true,
          receiptId: 'agent-settings-receipt',
          pathId: 'configuration.settings.apply',
          payloadSha256,
          appliedAtMs: Date.now(),
          rollbackAvailable: true,
          rollbackToken: 'agent-settings-rollback',
        };
      },
    });
    renderSystemApp('system-settings', '/configuration?view=agent', transport);
    const permissions = await screen.findByRole('radiogroup', {
      name: 'Agent 执行权限',
    });

    expect(within(permissions).getByRole('radio', { name: '全权限' })).toBeChecked();
    expect(within(permissions).getByRole('radio', { name: '全自动' })).toBeEnabled();
    await user.click(within(permissions).getByRole('radio', { name: '全自动' }));

    await waitFor(() => expect(transport.requests.map(({ request }) => request.pathId)).toEqual(expect.arrayContaining([
      'configuration.settings.preview',
      'configuration.settings.apply',
    ])));
    await waitFor(() => expect(within(permissions).getByRole('radio', { name: '全自动' })).toBeChecked());
    expect(settingsReads).toBeGreaterThanOrEqual(2);
  });

  it('hides local full-trust defaults when the surface is served by TeamGateway', async () => {
    const teamMeta = document.createElement('meta');
    teamMeta.name = 'paw-deployment';
    teamMeta.content = 'team';
    document.head.appendChild(teamMeta);
    const transport = baseTransport({
      'configuration.settings': agentPreferenceSettings('per_action'),
    });
    renderSystemApp('system-settings', '/configuration?view=agent', transport);

    const permissions = await screen.findByRole('radiogroup', { name: 'Agent 执行权限' });
    expect(within(permissions).getAllByRole('radio')).toHaveLength(1);
    expect(within(permissions).getByRole('radio', { name: '工作区托管（沙箱）' })).toBeChecked();
    expect(within(permissions).queryByRole('radio', { name: '全自动' })).not.toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.pathId === 'configuration.settings.preview')).toBe(false);
  });

  it.each([
    ['read_only', '只读（沙箱）'],
    ['workspace_managed', '工作区托管（沙箱）'],
  ] as const)('shows the saved %s Agent default without relabeling it as Full Access', async (executionMode, label) => {
    const transport = baseTransport({
      'configuration.settings': agentPreferenceSettings(executionMode),
    });
    renderSystemApp('system-settings', '/configuration?view=agent', transport);

    const permissions = await screen.findByRole('radiogroup', {
      name: 'Agent 执行权限',
    });
    expect(within(permissions).getAllByRole('radio')).toHaveLength(4);
    expect(within(permissions).getByRole('radio', { name: label })).toBeChecked();
    expect(within(permissions).getByRole('radio', { name: '全权限' })).not.toBeChecked();
  });

  it('keeps Package installation on validate, preview, explicit confirmation, and apply', async () => {
    const user = userEvent.setup();
    const transport = baseTransport({
      'agent.extensions.catalog': {
        ok: true,
        items: [{
          id: 'verified-package',
          displayName: 'Verified Package',
          description: 'Runtime catalog entry',
          publisher: 'Local registry',
          source: { kind: 'local', label: '本机目录' },
          permissions: ['workspace.read'],
          security: { notes: '来源已验证' },
          versions: [{ version: '1.0.0' }],
          latestVersion: '1.0.0',
          installed: false,
          updateAvailable: false,
          actionable: true,
        }],
      },
      'agent.extensions.validate': {
        ok: true,
        validationToken: 'validation-real',
        extension: { id: 'verified-package', displayName: 'Verified Package', version: '1.0.0' },
      },
      'agent.extensions.preview': {
        ok: true,
        previewToken: 'preview-real',
        payloadSha256: 'b'.repeat(64),
        summary: { action: 'install', pluginId: 'verified-package', displayName: 'Verified Package', permissions: ['workspace.read'] },
      },
      'agent.extensions.apply': { ok: true, receiptId: 'receipt-real' },
    });
    renderSystemApp('app-center', '/plugins?view=catalog', transport);

    await user.click(await screen.findByRole('button', { name: '查看安装内容' }));
    expect(await screen.findByText('等待你的确认')).toBeInTheDocument();
    expect(transport.requests.map(({ request }) => request.pathId)).toEqual(expect.arrayContaining([
      'agent.extensions.validate',
      'agent.extensions.preview',
    ]));
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.extensions.apply')).toBe(false);

    await user.click(screen.getByRole('button', { name: '确认更改' }));
    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'agent.extensions.apply')).toBe(true));
  });

  it('narrows the Package catalog to one decision track without hiding the full list', async () => {
    const user = userEvent.setup();
    const transport = baseTransport({
      'agent.extensions.catalog': {
        ok: true,
        items: [
          catalogItemStub('pkg-fresh', 'Fresh Package', { installed: false, updateAvailable: false }),
          catalogItemStub('pkg-stale', 'Stale Package', { installed: true, updateAvailable: true }),
          catalogItemStub('pkg-current', 'Current Package', { installed: true, updateAvailable: false }),
        ],
      },
    });
    renderSystemApp('app-center', '/plugins?view=catalog', transport);

    expect(await screen.findByText('Fresh Package')).toBeInTheDocument();
    expect(screen.getByText('Stale Package')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '有更新' }));
    expect(screen.getByText('Stale Package')).toBeInTheDocument();
    expect(screen.queryByText('Fresh Package')).not.toBeInTheDocument();
    expect(screen.queryByText('Current Package')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '可安装' }));
    expect(screen.getByText('Fresh Package')).toBeInTheDocument();
    expect(screen.queryByText('Stale Package')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '全部' }));
    expect(screen.getByText('Current Package')).toBeInTheDocument();
  });

  it('retains the Package filter and cached catalog after a refresh fails', async () => {
    const user = userEvent.setup();
    let readFails = false;
    const transport = baseTransport({
      'agent.extensions.catalog': () => {
        if (readFails) throw new Error('catalog unavailable');
        return { ok: true, items: [catalogItemStub('pkg-fresh', 'Fresh Package', { installed: false, updateAvailable: false })] };
      },
    });
    renderSystemApp('app-center', '/plugins?view=catalog', transport);
    const search = await screen.findByRole('textbox', { name: '搜索 Package 目录' });
    await user.type(search, 'Fresh');
    readFails = true;
    await user.click(screen.getByRole('button', { name: '刷新' }));

    expect(await screen.findByText('目录未能刷新，已保留上次结果')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '搜索 Package 目录' })).toBe(search);
    expect(search).toHaveValue('Fresh');
    expect(screen.getByText('Fresh Package')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '查看安装内容' })).toBeDisabled();

    readFails = false;
    await user.click(screen.getByRole('button', { name: '重试读取目录' }));
    await waitFor(() => expect(screen.queryByText('目录未能刷新，已保留上次结果')).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: '查看安装内容' })).toBeEnabled();
    expect(search).toHaveValue('Fresh');
  });

  it('keeps the successful Package receipt when follow-up catalog reads fail without applying twice', async () => {
    const user = userEvent.setup();
    let applied = false;
    let readFails = true;
    const transport = baseTransport({
      'agent.extensions.catalog': () => {
        if (applied && readFails) throw new Error('catalog unavailable after apply');
        return { ok: true, items: [catalogItemStub('pkg-fresh', 'Fresh Package', { installed: applied, updateAvailable: false })] };
      },
      'agent.extensions.validate': { ok: true, validationToken: 'validated-fresh' },
      'agent.extensions.preview': {
        ok: true, previewToken: 'preview-fresh', payloadSha256: 'b'.repeat(64),
        summary: { action: 'install', pluginId: 'pkg-fresh', displayName: 'Fresh Package', permissions: [] },
      },
      'agent.extensions.apply': () => {
        applied = true;
        return { ok: true, receipt: { receiptId: 'receipt-fresh', action: 'install' } };
      },
    });
    renderSystemApp('app-center', '/plugins?view=catalog', transport);
    await user.click(await screen.findByRole('button', { name: '查看安装内容' }));
    await user.click(await screen.findByRole('button', { name: '确认更改' }));

    expect(await screen.findByText('Package 更改已完成')).toBeInTheDocument();
    expect(screen.getByText(/receipt-fresh/)).toBeInTheDocument();
    expect(await screen.findByText('目录未能刷新，已保留上次结果')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '确认更改' })).not.toBeInTheDocument();
    expect(screen.queryByText('Package 操作未完成')).not.toBeInTheDocument();

    readFails = false;
    await user.click(screen.getByRole('button', { name: '重试读取目录' }));
    expect(await screen.findByRole('button', { name: '已是最新' })).toBeDisabled();
    expect(screen.getByText('Package 更改已完成')).toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.extensions.apply')).toHaveLength(1);
  });
});

function renderSystemApp(
  appId: PawSystemAppId,
  initialRoute: string,
  transport = baseTransport(),
) {
  return render(<SystemHarness appId={appId} initialRoute={initialRoute} transport={transport} />);
}

function groupLabels(): string[] {
  return [...document.querySelectorAll('.paw-system-app__nav-group')].map((node) => node.textContent?.trim() ?? '');
}

function catalogItemStub(
  id: string,
  displayName: string,
  state: { installed: boolean; updateAvailable: boolean },
) {
  return {
    id,
    displayName,
    description: `${displayName} 的说明`,
    publisher: 'Local registry',
    source: { kind: 'local', label: '本机目录' },
    permissions: [],
    security: { notes: '来源已验证' },
    versions: [{ version: '1.0.0' }],
    latestVersion: '1.0.0',
    actionable: true,
    ...state,
  };
}

function previewApprovalStub(approvalId: string, state: 'pending' | 'applied') {
  return {
    schemaVersion: 'rag-ime.agent-approval.v1',
    approvalId,
    sessionId: 'session-preview',
    toolCallId: `tool-call:${approvalId}`,
    toolId: 'workspace_shell',
    operation: 'run',
    payloadSha256: 'a'.repeat(64),
    preview: { summary: '示例请求' },
    riskLevel: 'R2',
    state,
    requestedAtMs: Date.now() - 1_000,
    expiresAtMs: Date.now() + 60_000,
    decidedBy: '',
  };
}

function SystemHarness({
  appId,
  initialRoute,
  transport,
}: {
  appId: PawSystemAppId;
  initialRoute: string;
  transport: MockControlTransport;
}) {
  const [route, setRoute] = useState(initialRoute);
  const [client] = useState(() => new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  }));
  return (
    <TooltipProvider delayDuration={0}>
      <QueryClientProvider client={client}>
        <ControlTransportProvider transport={transport}>
          <ThemeProvider>
            <PawOsAppearanceProvider>
              <MotionProvider>
                <PawOsDesktopProvider openRoute={setRoute} openWindow={() => undefined}>
                  <PawOsAppSurfaceProvider appId={appId} height={720} width={1_080}>
                    <PawSystemAppsMigrated appId={appId} initialRoute={route} />
                  </PawOsAppSurfaceProvider>
                </PawOsDesktopProvider>
              </MotionProvider>
            </PawOsAppearanceProvider>
          </ThemeProvider>
        </ControlTransportProvider>
      </QueryClientProvider>
    </TooltipProvider>
  );
}

function baseTransport(overrides: Record<string, unknown> = {}) {
  return new MockControlTransport({
    capabilities: {
      features: { managementWorkContract: true, configurationSettingsWorkContract: true },
    },
    routes: {
    'agent.tools.list': {
      schemaVersion: 'rag-ime.capability-catalog.v1',
      ok: true,
      revision: `sha256:${'a'.repeat(64)}`,
      effectiveAtMs: 1,
      projectScope: { supported: false, identityKind: 'none', reason: 'stable_project_identity_unavailable' },
      items: [],
    },
    'agent.configuration.get': {
      ok: true,
      configuration: {
        revision: 1,
        configuration: {
          sessionDefaults: { capabilityDisclosurePreferences: {} },
          capabilityDisclosure: { projectPreferences: {} },
        },
      },
    },
    'agent.extensions.list': { ok: true, runtimeAvailable: true, items: [] },
    'agent.extensions.catalog': { ok: true, items: [] },
    'agent.extensions.proposals': { ok: true, items: [] },
    'agent.lifecycleHooks.get': { ok: true, policies: [], recentEvents: [] },
    'agent.role.models': { ok: true, providers: [] },
    'configuration.settings': agentPreferenceSettings('per_action'),
    ...overrides,
  } });
}

function agentPreferenceSettings(executionMode: string) {
  return {
    ok: true,
    settings: {
      agent: {
        defaults: {
          modelReference: 'inherit',
          thinkingLevel: 'high',
          executionMode,
        },
      },
    },
    runtimeConfig: { runtimeRevision: 7 },
  };
}

function systemModelRoutingConfiguration(
  revision: number,
  overrides: Partial<Record<'primary' | 'traceDiagnostic' | 'toolAgent' | 'subagent' | 'roomCoordinator', {
    modelProfile: string;
    thinkingLevel: string;
  }>> = {},
) {
  const inherited = { modelProfile: 'inherit', thinkingLevel: 'inherit' };
  return {
    ok: true,
    configuration: {
      revision,
      configuration: {
        sessionDefaults: { capabilityDisclosurePreferences: {} },
        capabilityDisclosure: { projectPreferences: {} },
        modelRouting: {
          primary: { ...inherited, ...overrides.primary },
          traceDiagnostic: { ...inherited, ...overrides.traceDiagnostic },
          toolAgent: { ...inherited, ...overrides.toolAgent },
          subagent: { ...inherited, ...overrides.subagent },
          roomCoordinator: { ...inherited, ...overrides.roomCoordinator },
        },
      },
    },
  };
}
