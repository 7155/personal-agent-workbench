import { onlineManager, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { ControlTransportProvider } from '@/app/control-transport';
import { createPreviewTransport } from '@/app/preview-control-transport';
import { TooltipProvider } from '@/components/primitives';
import { PawOsAppSurfaceProvider } from '@/features/paw-os/surface-context';
import { pawExtensionApps } from '@/paw-os/extensions/registry';
import { controlRoute, type ControlPathId } from '@/platform/routes';
import type { ControlTransport } from '@/platform/transport';
import { MockControlTransport, type MockRouteHandler } from '@/test/mock-transport';
import { PluginsFeature } from '.';
import { requireCapabilityCatalog } from './capability-policy';

afterEach(() => {
  cleanup();
  onlineManager.setOnline(true);
});

describe('PluginsFeature', () => {
  it.each([
    { response: { schemaVersion: 'rag-ime.capability-catalog.v1', ok: true }, message: '能力目录数据不完整或格式异常' },
    { response: { schemaVersion: 'rag-ime.control-tool-list.v1', ok: true }, message: '能力目录版本不兼容' },
    { response: { ok: true }, message: '能力目录缺少版本信息' },
    { response: { ok: false }, message: '服务未能返回能力目录' },
  ])('identifies the actual catalog failure: $message', ({ response, message }) => {
    expect(() => requireCapabilityCatalog(response)).toThrow(message);
  });

  it.each(['invalid data', 'network'])('restores the same search after retrying a catalog %s failure', async (failure) => {
    const user = userEvent.setup();
    let reads = 0;
    const items = [tool({ id: 'memory', displayName: '记忆与工具书', description: '读取记忆' })];
    const valid = capabilityCatalog(items);
    const transport = renderPlugins({ 'agent.tools.list': () => {
      reads += 1;
      if (reads === 2) {
        if (failure === 'network') throw new Error('连接已断开，请重试。');
        return { ...valid, items: [{ ...items[0], description: undefined }] };
      }
      return valid;
    } }, '/plugins?view=capabilities', true);
    const search = await screen.findByRole('textbox', { name: '搜索' });
    await user.type(search, '记忆');
    await user.click(screen.getByRole('button', { name: '刷新' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(failure === 'network' ? '连接已断开' : '能力目录数据不完整或格式异常');
    expect(alert).not.toHaveTextContent('版本不匹配');
    expect(screen.queryByRole('button', { name: /记忆与工具书/ })).not.toBeInTheDocument();
    await user.click(within(alert).getByRole('button', { name: '重试' }));
    expect(await screen.findByRole('button', { name: /记忆与工具书/ })).toBeVisible();
    expect(screen.getByRole('textbox', { name: '搜索' })).toHaveValue('记忆');
    expect(reads).toBe(3);
    expect(transport.requests.every(({ request }) => controlRoute(request.pathId).method === 'GET')).toBe(true);
  });

  it('explains the actual Room operations and retains canonical names in technical details', async () => {
    const user = userEvent.setup();
    const operations = ['list', 'add_participant', 'remove_participant', 'delegate', 'delegate_batch', 'retry', 'accept', 'return', 'collect', 'wait', 'post', 'peer_list', 'peer_send', 'peer_ask', 'peer_reply'];
    const transport = renderPlugins({ 'agent.tools.list': capabilityCatalog([
      tool({ id: 'room_partner', displayName: 'Room 伙伴协作', description: '协作操作', operations }),
    ]) });
    await user.click(await screen.findByRole('button', { name: /Room 伙伴协作/ }));
    const detail = screen.getByRole('complementary', { name: '能力详情' });
    expect(within(detail).getByText('等待伙伴结果')).toBeVisible();
    expect(within(detail).getByText('收集伙伴结果')).toBeVisible();
    expect(within(detail).getByText('验收工作成果')).toBeVisible();
    expect(within(detail).getByText('退回工作成果')).toBeVisible();
    expect(within(detail).getByText('向伙伴提问')).toBeVisible();
    expect(within(detail).getByText('回复伙伴')).toBeVisible();
    expect(detail).not.toHaveTextContent('等待页面内容');
    expect(detail).not.toHaveTextContent('其他');
    await user.click(within(detail).getByText('查看技术参数'));
    for (const operation of operations) expect(detail).toHaveTextContent(`"${operation}"`);
    expect(transport.requests.every(({ request }) => controlRoute(request.pathId).method === 'GET')).toBe(true);
  });

  it('uses browser meanings for script execution, navigation and traces', async () => {
    const user = userEvent.setup();
    renderPlugins({ 'agent.tools.list': capabilityCatalog([
      tool({ id: 'browser', displayName: 'PAW Browser', description: '网页操作', operations: ['status', 'tabs', 'snapshot', 'screenshot', 'trace', 'run', 'navigate', 'back', 'forward', 'click', 'type', 'scroll', 'wait', 'stop'] }),
    ]) });
    await user.click(await screen.findByRole('button', { name: /PAW Browser/ }));
    const detail = screen.getByRole('complementary', { name: '能力详情' });
    for (const label of ['执行网页操作脚本', '查看浏览器操作记录', '后退一页', '前进一页', '等待页面内容']) {
      expect(within(detail).getByText(label)).toBeVisible();
    }
    expect(detail).not.toHaveTextContent('运行受控命令');
    expect(detail).not.toHaveTextContent('查看来源链路');
    expect(detail).not.toHaveTextContent('其他');
  });

  it('does not infer built-in operation meanings from a third-party display name', async () => {
    const user = userEvent.setup();
    renderPlugins({ 'agent.tools.list': capabilityCatalog([
      tool({ id: 'third_party', displayName: 'Room 伙伴协作', description: '协作操作', operations: ['wait', 'accept', 'vendor.custom'] }),
    ]) });
    await user.click(await screen.findByRole('button', { name: /Room 伙伴协作/ }));
    const detail = screen.getByRole('complementary', { name: '能力详情' });
    expect(detail).not.toHaveTextContent('等待伙伴结果');
    expect(detail).not.toHaveTextContent('等待页面内容');
    expect(detail).toHaveTextContent('其他 3 项操作');
    await user.click(within(detail).getByText('查看技术参数'));
    expect(detail).toHaveTextContent('vendor.custom');
  });

  it('uses the Runtime rollback target and still previews the canonical package action', async () => {
    const user = userEvent.setup();
    const transport = renderPlugins({
      'agent.extensions.list': { ok: true, items: [{
        id: '@paw/zhanggui-wenshu', displayName: '@paw/zhanggui-wenshu', version: '0.2.0', enabled: true,
        rollbackAvailable: true, rollbackTarget: { version: '0.1.1', digest: 'target-digest' },
        previousVersion: '0.0.1', source: { kind: 'managed' },
      }, {
        id: 'unknown-target', displayName: 'Unknown target', enabled: true, rollbackAvailable: true,
      }] },
    }, '/plugins', true);
    const card = await screen.findByRole('article', { name: '掌柜问数 Package' });
    expect(card).toHaveTextContent('可恢复到 v0.1.1');
    expect(card).not.toHaveTextContent('没有可恢复的历史版本');
    expect(card).not.toHaveTextContent('v0.0.1');
    const unknown = screen.getByRole('article', { name: 'Unknown target Package' });
    expect(unknown).toHaveTextContent('可恢复上一版本，具体版本将在预览中显示');
    await user.click(within(card).getByRole('button', { name: '恢复上一版本' }));
    await waitFor(() => expect(transport.requests.find(({ request }) =>
      request.pathId === 'agent.extensions.preview',
    )?.request.body).toEqual({ action: 'rollback', pluginId: '@paw/zhanggui-wenshu' }));
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.extensions.apply')).toBe(false);
  });

  it('explains known installed packages and preserves original fields in an explicit disclosure', async () => {
    const user = userEvent.setup();
    renderPlugins({ 'agent.extensions.list': { ok: true, items: [{
      id: '@paw/pi-session-workflow', displayName: '@paw/pi-session-workflow',
      description: 'Optional Session-local Goal, Plan, Todo, and Workflow controls for Pi',
      version: '0.1.0', enabled: true, permissions: ['sandbox.run', 'vendor.custom'],
      source: { kind: 'managed', requested: 'npm:@paw/pi-session-workflow' },
      resources: { extensions: ['session_workflow'], skills: ['workflow'] },
      privatePath: '/private/should-not-be-disclosed',
    }, { id: 'third-party', displayName: 'Third Party', description: 'Author supplied description.' }] } }, '/plugins', true);
    const card = await screen.findByRole('article', { name: '对话工作流 Package' });
    expect(card).toHaveTextContent('管理当前对话的目标、计划、任务清单和工作流程');
    expect(card).toHaveTextContent('由运行环境管理');
    expect(card).toHaveTextContent('在受控环境中执行');
    expect(card).toHaveTextContent('vendor.custom');
    expect(card).toHaveTextContent('扩展 1 · 技能 1');
    expect(card).not.toHaveTextContent('@paw/pi-session-workflow');
    expect(card).not.toHaveTextContent('Session-local');
    await user.click(within(card).getByText('包标识与原始信息'));
    expect(card).toHaveTextContent('@paw/pi-session-workflow');
    expect(card).toHaveTextContent('Session-local');
    expect(card).toHaveTextContent('sandbox.run');
    expect(card).not.toHaveTextContent('/private/should-not-be-disclosed');
    expect(screen.getByRole('article', { name: 'Third Party Package' })).toHaveTextContent('Author supplied description.');
  });

  it('waits for each inventory before showing a zero count and accepts a real empty result', async () => {
    let resolvePackages!: (value: unknown) => void;
    let resolveHooks!: (value: unknown) => void;
    const packages = new Promise((resolve) => { resolvePackages = resolve; });
    const hooks = new Promise((resolve) => { resolveHooks = resolve; });
    renderPlugins({ 'agent.extensions.list': () => packages, 'agent.lifecycleHooks.get': () => hooks }, '/plugins', true);
    const installed = await screen.findByRole('region', { name: '插件安装与更新' });
    const lifecycle = screen.getByRole('region', { name: '自动整理与提醒' });
    expect(installed).toHaveTextContent('正在读取');
    expect(installed).not.toHaveTextContent('0 个已安装');
    expect(lifecycle).toHaveTextContent('正在读取');
    expect(lifecycle).not.toHaveTextContent('0/0 已启用');
    await act(async () => { resolvePackages({ ok: true, runtimeAvailable: true, items: [] }); });
    expect(await within(installed).findByText('0 个已安装')).toBeVisible();
    expect(lifecycle).not.toHaveTextContent('0/0 已启用');
    await act(async () => { resolveHooks({ ok: true, policies: [], recentEvents: [] }); });
    expect(await within(lifecycle).findByText('0/0 已启用')).toBeVisible();
  });

  it('reports unavailable inventories without fabricating empty counts', async () => {
    renderPlugins({
      'agent.extensions.list': () => { throw new Error('inventory unavailable'); },
      'agent.lifecycleHooks.get': () => { throw new Error('hooks unavailable'); },
    }, '/plugins', true);
    const installed = await screen.findByRole('region', { name: '插件安装与更新' });
    const lifecycle = screen.getByRole('region', { name: '自动整理与提醒' });
    expect(await within(installed).findByText('暂时无法读取')).toBeVisible();
    expect(await within(lifecycle).findByText('状态不可用')).toBeVisible();
    expect(installed).not.toHaveTextContent('0 个已安装');
    expect(lifecycle).not.toHaveTextContent('0/0 已启用');
  });

  it('searches displayed capability names and ranks them ahead of description matches', async () => {
    const user = userEvent.setup();
    renderPlugins({ 'agent.tools.list': capabilityCatalog([
      tool({ id: 'overview', displayName: '控制中心概览', description: '查看模型和记忆状态' }),
      tool({ id: 'memory', displayName: '个人上下文记忆', description: '查询记忆' }),
      tool({ id: 'ask', displayName: 'Ask', description: '向用户提出选择' }),
    ]) });
    const list = await screen.findByRole('group', { name: '能力列表' });
    const search = screen.getByRole('textbox', { name: '搜索' });
    await user.type(search, '记忆');
    expect(within(list).getAllByRole('button')[0]).toHaveTextContent('个人上下文记忆');
    await user.clear(search);
    await user.type(search, '向你提问');
    expect(within(list).getAllByRole('button')).toHaveLength(1);
    expect(within(list).getByRole('button', { name: /向你提问/ })).toBeVisible();
    await user.clear(search);
    await user.type(search, 'tool:ask');
    expect(within(list).getByRole('button', { name: /向你提问/ })).toBeVisible();
  });

  it('explains capability use and permissions while preserving the original contract on demand', async () => {
    const user = userEvent.setup();
    renderPlugins({ 'agent.tools.list': capabilityCatalog([
      tool({ id: 'room_partner', displayName: 'Room 伙伴协作', description: '在 Room 中分派 WorkItem；恢复时携带 expectedRevision 和 retry。', riskLevel: 'R1' }),
    ]) });
    const row = await screen.findByRole('button', { name: /Room 伙伴协作/ });
    expect(row).not.toHaveTextContent('WorkItem');
    expect(row).toHaveTextContent('分派工作');
    await user.click(row);
    const detail = screen.getByRole('complementary', { name: '能力详情' });
    expect(detail).toHaveTextContent('遵循对话的执行权限');
    expect(detail).toHaveTextContent('由具体对话的权限决定');
    expect(detail).not.toHaveTextContent('native_approval');
    expect(detail).not.toHaveTextContent('expectedRevision');
    await user.click(within(detail).getByText('查看技术参数'));
    expect(detail).toHaveTextContent('tool:room_partner');
    expect(detail).toHaveTextContent('expectedRevision');
    expect(detail).toHaveTextContent('native_approval');
  });

  it('distinguishes a missing Room context from a disabled or offline feature', async () => {
    const user = userEvent.setup();
    const room = tool({ id: 'room_partner', displayName: 'Room 伙伴协作', description: 'Room 工作协作', alwaysAvailable: true });
    renderPlugins({ 'agent.tools.list': capabilityCatalog([{
      ...room,
      status: 'offline',
      disclosure: { ...room.disclosure, effective: 'disabled' as const, state: 'hidden' as const, reason: 'room_context_required' },
      reasons: ['room_context_required'],
    }]) });
    const row = await screen.findByRole('button', { name: /Room 伙伴协作/ });
    expect(row).toHaveTextContent('进入 Room 后可用');
    expect(row).not.toHaveTextContent('暂时离线');
    expect(row).not.toHaveTextContent('已关闭');
    expect(screen.getByText(/1 项功能/)).toHaveTextContent('0 项已关闭');
    await user.click(row);
    const detail = screen.getByRole('complementary', { name: '能力详情' });
    expect(within(detail).getByRole('region', { name: '固定能力策略' })).toHaveTextContent('进入 Room 后可用');
    expect(within(detail).queryByRole('combobox')).not.toBeInTheDocument();
    await user.click(screen.getByRole('combobox', { name: '状态' }));
    await user.click(screen.getByRole('option', { name: '已关闭' }));
    expect(screen.queryByRole('button', { name: /Room 伙伴协作/ })).not.toBeInTheDocument();
  });

  it('gives native function switches their own page without package installation controls', async () => {
    renderPlugins({}, '/plugins?view=capabilities&capability=tool%3Amemory', true);
    const capabilities = await screen.findByRole('region', { name: '功能开关' });
    expect(await within(capabilities).findByRole('group', { name: '能力列表' })).toBeVisible();
    expect(within(capabilities).getByRole('complementary', { name: '能力详情' })).toHaveTextContent('记忆与工具书');
    expect(screen.queryByRole('textbox', { name: 'Pi Package 来源' })).not.toBeInTheDocument();
  });

  it('opens the linked capability and separates availability from its default use', async () => {
    renderPlugins({}, '/plugins?capability=tool%3Amemory');
    const detail = await screen.findByRole('complementary', { name: '能力详情' });
    expect(within(detail).getByRole('combobox', { name: '记忆与工具书的所有对话默认' })).toBeVisible();
    expect(await screen.findByText('正在设置所有对话的默认功能')).toBeVisible();
    const list = screen.getByRole('group', { name: '能力列表' });
    const memory = within(list).getByRole('button', { name: /记忆与工具书/ });
    expect(memory).toHaveTextContent('连接正常');
    expect(memory).toHaveTextContent('已启用');
  });

  it('does not query or poll while its PAWOS window is inactive', async () => {
    const transport = renderPlugins({}, '/plugins', true, false);

    await act(async () => { await Promise.resolve(); });

    expect(transport.requests).toHaveLength(0);
    expect(within(screen.getByRole('region', { name: '插件安装与更新' })).getByText('等待读取')).toBeVisible();
    expect(within(screen.getByRole('region', { name: '自动整理与提醒' })).getByText('等待读取')).toBeVisible();
  });

  it('keeps the setting scope and precedence visible above the capability list', async () => {
    renderPlugins();
    expect(await screen.findByText('正在设置所有对话的默认功能')).toBeVisible();
    expect(screen.getByText('对话单独设置优先，其次是项目设置，最后是所有对话默认。更改从下一轮生效。')).toBeVisible();
    expect(screen.queryByText('能力如何生效')).not.toBeInTheDocument();
  });

  it('keeps large capability schemas behind a real second-level disclosure', async () => {
    const user = userEvent.setup();
    renderPlugins({
      'agent.tools.list': capabilityCatalog([
        tool({
          id: 'workspace_read',
          displayName: '工作区读取',
          description: '读取已授权工作区中的文本',
          domain: 'workspace',
          riskLevel: 'R0',
          operations: ['read'],
          sessionModes: ['assistant'],
          parameters: { absolutePath: { type: 'string', description: '工作区内的绝对路径' } },
        }),
      ]),
    });

    await user.click(await screen.findByRole('button', { name: /工作区读取/ }));
    expect(screen.queryByText(/absolutePath/)).not.toBeInTheDocument();
    await user.click(screen.getByText('查看技术参数'));
    expect(screen.getByText(/absolutePath/)).toBeVisible();
    expect(screen.getByText(/工作区内的绝对路径/)).toBeVisible();
  });

  it('shows the routed proposal workbench without the legacy App Center shell', async () => {
    const user = userEvent.setup();
    renderPlugins({
      'agent.extensions.proposals': {
        ok: true,
        items: [{
          proposalId: 'proposal-disable',
          previewToken: 'preview-disable',
          payloadSha256: 'b'.repeat(64),
          summary: {
            action: 'disable',
            pluginId: 'session-review',
            displayName: 'Session Review',
            version: '1.1.0',
            permissions: ['session.read'],
            recommendationReason: '现有复盘能力与请求完全匹配',
            dependencies: ['@paw/pi-session-workflow'],
            risks: ['停用后仅影响新对话'],
            capabilityOverlap: ['与内置只读检查部分重叠'],
            verificationPlan: ['新建对话并检查 Skill 目录'],
          },
        }],
      },
    }, '/plugins?view=proposals', true);

    expect(await screen.findByRole('heading', { name: /的建议/ })).toBeInTheDocument();
    const proposal = await screen.findByRole('button', { name: /对话复盘/ });
    expect(proposal).toHaveTextContent('停用插件 · v1.1.0 · 1 项权限');
    await user.click(proposal);
    const approval = screen.getByRole('region', { name: '待确认的插件更改' });
    expect(approval).toHaveTextContent('现有复盘能力与请求完全匹配');
    expect(approval).toHaveTextContent('@paw/pi-session-workflow');
    expect(approval).toHaveTextContent('停用后仅影响新对话');
    expect(approval).toHaveTextContent('与内置只读检查部分重叠');
    expect(approval).toHaveTextContent('新建对话并检查 Skill 目录');
    expect(screen.queryByText('能力概览')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Pi Package 来源')).not.toBeInTheDocument();
    expect(document.querySelector('.mgmt-page__header')).not.toBeInTheDocument();
    expect(document.querySelector('.mgmt-section')).not.toBeInTheDocument();
  });

  it('composes the native installed page as capability, Package and curation consoles without duplicated chrome', async () => {
    renderPlugins({
      'agent.extensions.list': {
        schemaVersion: 'rag-ime.plugin-inventory.v1',
        ok: true,
        runtimeAvailable: true,
        items: [{
          id: '@paw/pi-session-workflow',
          displayName: 'Session Workflow',
          version: '1.0.0',
          enabled: true,
          installed: true,
          rollbackAvailable: false,
          permissions: ['session.read'],
          resources: { skills: ['skills/session-workflow/SKILL.md'], prompts: ['prompts/workflow.md'] },
          source: { kind: 'npm', requested: 'npm:@paw/pi-session-workflow@1.0.0' },
        }],
      },
    }, '/plugins', true);

    // The capability browser and the curation hooks are now reachable in the
    // native App Center instead of remaining web-only functions.
    expect(screen.queryByRole('group', { name: '能力列表' })).not.toBeInTheDocument();
    expect(await screen.findByRole('button', { name: '管理 Agent 功能开关与默认设置' })).toBeVisible();

    const packages = await screen.findByRole('region', { name: '插件安装与更新' });
    const packageCard = await within(packages).findByRole('article', { name: 'Session Workflow Package' });
    expect(packageCard).toHaveTextContent('读取对话内容');
    expect(packageCard).toHaveTextContent('2 项资源');
    expect(packageCard).toHaveTextContent('npm 包');
    expect(packageCard).toHaveTextContent('没有可恢复的历史版本');
    expect(within(packages).getByRole('textbox', { name: 'Pi Package 来源' })).toBeInTheDocument();

    const curation = await screen.findByRole('region', { name: '自动整理与提醒' });
    expect(await within(curation).findByRole('switch', { name: '任务完成：已启用' })).toBeInTheDocument();

    // Redundant chrome stays out: no management-sheet header/sections, no
    // maintenance toggle, and no catalog rows duplicating the 目录 page.
    expect(document.querySelector('.mgmt-page__header')).not.toBeInTheDocument();
    expect(document.querySelector('.mgmt-section')).not.toBeInTheDocument();
    expect(screen.queryByText('能力概览')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '管理扩展与自动整理' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '查看安装内容' })).not.toBeInTheDocument();
    expect(screen.queryByText('Session Review')).not.toBeInTheDocument();
  });

  it('distinguishes enabled, loaded and invoked Package status with privacy-safe usage statistics', async () => {
    renderPlugins({
      'agent.extensions.list': {
        schemaVersion: 'rag-ime.plugin-inventory.v1',
        ok: true,
        runtimeAvailable: true,
        items: [{
          id: 'pi-lens',
          displayName: 'Pi Lens',
          version: '4.1.2',
          enabled: true,
          installed: true,
          rollbackAvailable: true,
          previousVersion: '4.1.1',
          permissions: ['workspace.read'],
          resources: { extensions: ['lens_diagnostics'], skills: ['skills/pi-lens/SKILL.md'] },
          source: { kind: 'npm', requested: 'npm:pi-lens@4.1.2' },
          usage: {
            loadedSessionCount: 3,
            lastLoadedAtMs: 1_787_784_720_000,
            invocationCount: 18,
            succeededCount: 17,
            failedCount: 1,
            cancelledCount: 0,
            averageDurationMs: 240,
            lastInvocation: {
              resourceKind: 'tool',
              resourceName: 'lens_diagnostics',
              sessionId: 'session-private-id',
              startedAtMs: 1_787_790_720_000,
              completedAtMs: 1_787_790_720_240,
              durationMs: 240,
              status: 'succeeded',
            },
          },
        }],
      },
    }, '/plugins', true);

    const packageCard = await screen.findByRole('article', { name: 'Pi Lens Package' });
    expect(packageCard).toHaveTextContent('已启用');
    expect(packageCard).toHaveTextContent('已加载 · 3 个对话');
    expect(packageCard).toHaveTextContent('已调用 · lens_diagnostics');
    expect(packageCard).toHaveTextContent('18 次调用');
    expect(packageCard).toHaveTextContent('成功 17 · 失败 1 · 取消 0');
    expect(packageCard).toHaveTextContent('平均 240 ms');
    expect(packageCard).toHaveTextContent('最近调用成功 · 240 ms');
    expect(packageCard).not.toHaveTextContent('session-private-id');
  });

  it('offers a guarded update on an installed Package when the catalog reports a newer version', async () => {
    const user = userEvent.setup();
    const transport = renderPlugins({
      'agent.extensions.list': {
        schemaVersion: 'rag-ime.plugin-inventory.v1',
        ok: true,
        runtimeAvailable: true,
        items: [{
          id: 'session-review',
          displayName: 'Session Review',
          version: '1.0.0',
          enabled: true,
          installed: true,
          rollbackAvailable: false,
          permissions: ['session.read'],
          resources: { skills: ['skills/session-review/SKILL.md'] },
          source: { kind: 'bundled', requested: 'session-review' },
        }],
      },
      'agent.extensions.catalog': {
        ok: true,
        items: [{
          id: 'session-review',
          displayName: 'Session Review',
          description: '基于事实审阅会话结果',
          publisher: 'Personal Agent Workbench',
          source: { kind: 'bundled', label: 'Product bundle' },
          permissions: ['session.read'],
          security: { notes: '只读会话权限' },
          versions: [{ version: '1.1.0' }, { version: '1.0.0' }],
          latestVersion: '1.1.0',
          installed: true,
          updateAvailable: true,
          actionable: true,
        }],
      },
      'agent.extensions.preview': {
        ok: true,
        previewToken: 'preview-update-token',
        payloadSha256: 'd'.repeat(64),
        summary: { action: 'update', pluginId: 'session-review', displayName: 'Session Review', version: '1.1.0' },
      },
    }, '/plugins', true);

    const packageCard = await screen.findByRole('article', { name: '对话复盘 Package' });
    expect(within(packageCard).getByText('有更新')).toBeInTheDocument();

    await user.click(within(packageCard).getByRole('button', { name: '更新到 v1.1.0' }));
    await waitFor(() => expect(transport.requests.some((call) => (
      call.request.pathId === 'agent.extensions.validate'
    ))).toBe(true));
    expect(transport.requests.find((call) => (
      call.request.pathId === 'agent.extensions.validate'
    ))?.request.body).toEqual({ catalogId: 'session-review', catalogVersion: '1.1.0' });
    expect(transport.requests.find((call) => (
      call.request.pathId === 'agent.extensions.preview'
    ))?.request.body).toEqual({ action: 'update', validationToken: 'validation-token', enable: true });

    const approval = await screen.findByRole('region', { name: '待确认的插件更改' });
    expect(within(approval).getByText('更新插件：对话复盘')).toBeVisible();
    expect(transport.requests.some((call) => call.request.pathId === 'agent.extensions.apply')).toBe(false);

    await user.click(screen.getByRole('button', { name: '确认更改' }));
    await waitFor(() => expect(transport.requests.filter((call) => (
      call.request.pathId === 'agent.extensions.apply'
    ))).toHaveLength(1));
    expect(await screen.findByText('更改已应用')).toBeVisible();
    expect(screen.getByText('更新插件：对话复盘')).toBeVisible();
  });

  it('keeps a stable catalog split while opening and closing capability details', async () => {
    const user = userEvent.setup();
    renderPlugins();

    expect(await screen.findByRole('heading', { name: '插件管理', level: 1 })).toBeInTheDocument();
    const list = await screen.findByRole('group', { name: '能力列表' });
    const descriptions = Array.from(list.querySelectorAll<HTMLElement>('.plugins-list__copy > span'));
    expect(descriptions).toHaveLength(4);
    expect(descriptions.every((description) => {
      const style = getComputedStyle(description);
      return style.display === 'block'
        && style.overflow === 'visible'
        && style.webkitLineClamp !== '2';
    })).toBe(true);
    expect(within(list).getByRole('button', { name: /记忆与工具书/ })).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByRole('complementary', { name: '能力详情占位' })).toBeInTheDocument();
    expect(list.parentElement).toHaveAttribute('data-detail-open', 'false');

    const detailTrigger = within(list).getByRole('button', { name: /历史与配置/ });
    await user.click(detailTrigger);
    const detail = screen.getByRole('complementary', { name: '能力详情' });
    expect(list.parentElement).toHaveAttribute('data-detail-open', 'true');
    expect(detail).toHaveTextContent('受禁区保护');
    expect(detail).toHaveTextContent('遵循对话的执行权限');
    expect(detail).not.toHaveTextContent('native_approval');
    expect(detail).toHaveTextContent('恢复备份');
    expect(detail).toHaveTextContent('执行授权');
    expect(detail).toHaveTextContent('伙伴可见范围');
    expect(within(detail).getByRole('combobox', { name: '历史与配置的所有对话默认' })).toBeEnabled();

    expect(document.body).not.toHaveTextContent('configuration');
    expect(document.body).not.toHaveTextContent('restore_apply');
    expect(document.body).not.toHaveTextContent('/api/private/tools');
    expect(document.body).not.toHaveTextContent('R3');
    expect(document.body).not.toHaveTextContent('assistant');
    expect(document.body).not.toHaveTextContent('coordinator');

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('complementary', { name: '能力详情' })).not.toBeInTheDocument();
    expect(detailTrigger).toHaveFocus();

    await user.click(detailTrigger);
    await user.click(screen.getByRole('button', { name: '关闭能力详情' }));
    expect(screen.queryByRole('complementary', { name: '能力详情' })).not.toBeInTheDocument();
    expect(screen.getByRole('complementary', { name: '能力详情占位' })).toBeInTheDocument();
    expect(list.parentElement).toHaveAttribute('data-detail-open', 'false');
  });

  it('shows fixed base tools without persistent disclosure controls', async () => {
    const user = userEvent.setup();
    const fixedAsk = tool({
      id: 'ask',
      displayName: 'Ask',
      description: '向用户提出仍需其决定的结构化选择',
      domain: 'planning',
      riskLevel: 'R0',
      operations: ['ask'],
      sessionModes: ['assistant', 'coordinator'],
      alwaysAvailable: true,
    });
    const transport = renderPlugins({
      'agent.tools.list': capabilityCatalog([fixedAsk]),
    });

    await user.click(await screen.findByRole('button', { name: /向你提问/ }));
    const detail = screen.getByRole('complementary', { name: '能力详情' });
    expect(within(detail).getByRole('region', { name: '固定能力策略' })).toHaveTextContent('默认可用');
    expect(within(detail).queryByRole('combobox', { name: 'Ask的所有对话默认' })).not.toBeInTheDocument();
    expect(transport.requests.some((call) => call.request.pathId === 'agent.configuration.update')).toBe(false);
  });

  it('reports an installed backend catalog version mismatch and never renders legacy items as controls', async () => {
    const user = userEvent.setup();
    const transport = renderPlugins({
      'agent.tools.list': {
        schemaVersion: 'rag-ime.control-tool-list.v1',
        ok: true,
        items: [{ id: 'legacy-tool', displayName: '旧工具' }],
      },
    });

    expect(await screen.findByText(/服务返回 rag-ime\.control-tool-list\.v1/)).toBeVisible();
    expect(screen.queryByRole('button', { name: /旧工具/ })).not.toBeInTheDocument();
    const before = transport.requests.filter((call) => call.request.pathId === 'agent.tools.list').length;
    await user.click(screen.getByRole('button', { name: '重试' }));
    await waitFor(() => expect(transport.requests.filter((call) => call.request.pathId === 'agent.tools.list')).toHaveLength(before + 1));
  });

  it('opens the in-OS studio from the installed capability page', async () => {
    const user = userEvent.setup();
    renderPlugins();
    await user.click(await screen.findByRole('button', { name: '管理扩展与自动整理' }));
    await user.click(await screen.findByRole('button', { name: '获取或制作能力' }));

    expect(screen.getByTestId('test-location')).toHaveTextContent('/plugins?view=studio');
    expect(screen.getByRole('region', { name: '制作 App 与插件' })).toBeInTheDocument();
  });

  it('resolves an npm Pi Package and stops at the product confirmation card', async () => {
    const user = userEvent.setup();
    const transport = renderPlugins({
      'agent.extensions.validate': {
        ok: true,
        validationToken: 'package-validation-token',
        distribution: 'pi_package',
        extension: {
          id: 'example.context-helper',
          displayName: 'Context Helper',
          version: '1.2.3',
          resources: {
            extensions: [],
            skills: ['skills/context-helper/SKILL.md'],
            prompts: ['prompts/context.md'],
            themes: [],
          },
          source: { kind: 'npm', requested: 'npm:@example/context-helper@1.2.3' },
        },
      },
      'agent.extensions.preview': {
        ok: true,
        previewToken: 'package-preview-token',
        payloadSha256: 'c'.repeat(64),
        summary: {
          action: 'install',
          pluginId: 'example.context-helper',
          displayName: 'Context Helper',
          version: '1.2.3',
          resources: {
            extensions: [],
            skills: ['skills/context-helper/SKILL.md'],
            prompts: ['prompts/context.md'],
            themes: [],
          },
        },
      },
    });

    await user.click(await screen.findByRole('button', { name: '管理扩展与自动整理' }));
    await user.type(
      screen.getByRole('textbox', { name: 'Pi Package 来源' }),
      'npm:@example/context-helper@1.2.3',
    );
    const inspectPackage = screen.getByRole('button', { name: '检查并预览' });
    expect(inspectPackage).toBeEnabled();
    await user.click(inspectPackage);

    await waitFor(() => expect(transport.requests.some((call) => (
      call.request.pathId === 'agent.extensions.validate'
    ))).toBe(true));
    expect(transport.requests.find((call) => (
      call.request.pathId === 'agent.extensions.validate'
    ))?.request.body).toEqual({ packageSource: 'npm:@example/context-helper@1.2.3' });
    expect(await screen.findByText('Pi 已解析')).toBeVisible();
    expect(screen.getByText(/2 项资源/)).toBeVisible();
    expect(screen.getByText('等待你的批准')).toBeVisible();
    expect(transport.requests.some((call) => (
      call.request.pathId === 'agent.extensions.preview'
      && JSON.stringify(call.request.body).includes('package-validation-token')
    ))).toBe(true);
    expect(transport.requests.some((call) => call.request.pathId === 'agent.extensions.apply')).toBe(false);
  });

  it('filters capabilities by readable purpose, availability and kind', async () => {
    const user = userEvent.setup();
    renderPlugins();
    await screen.findByRole('heading', { name: '插件管理', level: 1 });

    const search = await screen.findByRole('textbox', { name: '搜索' });
    await user.type(search, '语音');
    expect(screen.getByRole('button', { name: /语音输入/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /记忆与工具书/ })).not.toBeInTheDocument();

    await user.clear(search);
    await user.click(screen.getByRole('combobox', { name: '状态' }));
    await user.click(await screen.findByRole('option', { name: '需要处理' }));
    expect(screen.getByRole('button', { name: /工作区读取/ })).toBeInTheDocument();
    await user.click(screen.getByRole('combobox', { name: '状态' }));
    await user.click(await screen.findByRole('option', { name: '全部状态' }));
    await user.click(screen.getByRole('radio', { name: '技能' }));
    expect(screen.queryByRole('button', { name: /工作区读取/ })).not.toBeInTheDocument();
  });

  it('re-reads the backend catalog and defaults after connectivity returns', async () => {
    let catalogReads = 0;
    let defaultReads = 0;
    renderPlugins({
      'agent.tools.list': () => {
        catalogReads += 1;
        return capabilityCatalog(toolItems());
      },
      'agent.configuration.get': () => {
        defaultReads += 1;
        return capabilityDefaults();
      },
    });

    await waitFor(() => {
      expect(catalogReads).toBe(1);
      expect(defaultReads).toBe(1);
    });
    act(() => onlineManager.setOnline(false));
    act(() => onlineManager.setOnline(true));
    await waitFor(() => {
      expect(catalogReads).toBe(2);
      expect(defaultReads).toBe(2);
    });
  });

  it('persists global disclosure defaults without changing authorization', async () => {
    const user = userEvent.setup();
    const transport = renderPlugins();
    await user.click(await screen.findByRole('button', { name: /记忆与工具书/ }));
    const preference = screen.getByRole('combobox', {
      name: '记忆与工具书的所有对话默认',
    });
    await user.click(preference);
    await user.click(await screen.findByRole('option', { name: '关闭' }));

    await waitFor(() => expect(transport.requests.some((call) => (
      call.request.pathId === 'agent.configuration.update'
      && typeof call.request.body === 'object'
      && call.request.body !== null
      && !Array.isArray(call.request.body)
      && JSON.stringify(call.request.body).includes('"tool:memory":"disabled"')
    ))).toBe(true));
    const detail = screen.getByRole('complementary', { name: '能力详情' });
    expect(detail).toHaveTextContent('由具体对话的权限决定');
    await user.click(within(detail).getByText('查看技术参数'));
    expect(detail).toHaveTextContent('"state": "not_applicable"');
    expect(await screen.findByText('默认设置已保存')).toBeVisible();
    expect(screen.getByText(/所有对话默认已保存/)).toBeVisible();
  });

  it('loads and persists a project default from an Agent Session context', async () => {
    const user = userEvent.setup();
    const projectId = `workspace-${'b'.repeat(64)}`;
    const transport = renderPlugins({
      'agent.tools.list': capabilityCatalog(toolItems(), {
        supported: true,
        identityKind: 'workspace_scope_sha256',
        projectId,
        reason: 'session_workspace_scope',
      }, 'session-project'),
      'agent.configuration.get': capabilityDefaults({
        [projectId]: { 'tool:memory': 'enabled' },
      }),
    }, '/plugins?sessionId=session-project');

    await user.click(await screen.findByRole('button', { name: /记忆与工具书/ }));
    const projectPreference = screen.getByRole('combobox', {
      name: '记忆与工具书的当前项目默认',
    });
    expect(projectPreference).toHaveTextContent('启用');
    expect(screen.getByRole('region', { name: '能力可见范围' })).toHaveTextContent('适用于此项目中跟随默认的对话');
    await user.click(projectPreference);
    await user.click(await screen.findByRole('option', { name: '关闭' }));

    await waitFor(() => expect(transport.requests.some((call) => (
      call.request.pathId === 'agent.configuration.update'
      && JSON.stringify(call.request.body).includes(`"${projectId}"`)
      && JSON.stringify(call.request.body).includes('"tool:memory":"disabled"')
    ))).toBe(true));
    expect(transport.requests.find((call) => call.request.pathId === 'agent.tools.list')?.request.query)
      .toEqual({ sessionId: 'session-project' });
    expect(await screen.findByText('默认设置已保存')).toBeVisible();
    expect(screen.getByText(/当前项目默认已保存/)).toBeVisible();
  });

  it('keeps the failed persistent change owner-scoped and retries the same preference', async () => {
    let attempts = 0;
    const user = userEvent.setup();
    const transport = renderPlugins({
      'agent.configuration.update': () => {
        attempts += 1;
        if (attempts === 1) throw new Error('配置修订冲突');
        return { ok: true };
      },
    });

    await user.click(await screen.findByRole('button', { name: /记忆与工具书/ }));
    await user.click(screen.getByRole('combobox', { name: '记忆与工具书的所有对话默认' }));
    await user.click(await screen.findByRole('option', { name: '关闭' }));
    expect(await screen.findByText('默认设置没有保存')).toBeVisible();
    expect(screen.getAllByText('配置修订冲突').length).toBeGreaterThan(0);

    await user.click(screen.getAllByRole('button', { name: '重试这次更改' })[0]!);
    await waitFor(() => expect(attempts).toBe(2));
    expect(transport.requests.filter((call) => call.request.pathId === 'agent.configuration.update')).toHaveLength(2);
    expect(await screen.findByText('默认设置已保存')).toBeVisible();
  });

  it('validates, previews and explicitly applies a first-party catalog plugin', async () => {
    const user = userEvent.setup();
    const transport = renderPlugins();
    await user.click(await screen.findByRole('button', { name: '管理扩展与自动整理' }));
    await user.click(await screen.findByRole('button', { name: '查看安装内容' }));
    expect(transport.filePickCalls).toEqual([]);

    const approval = await screen.findByRole('region', { name: '待确认的插件更改' });
    expect(within(approval).getByText('等待你的批准')).toBeVisible();
    const stages = within(approval).getByRole('list', { name: '生命周期进度' });
    expect(within(stages).getByText('检查来源')).toBeVisible();
    expect(within(stages).getByText('你的确认')).toHaveAttribute('aria-current', 'step');
    expect(within(stages).getByText('应用并出具回执')).toBeVisible();

    await user.click(screen.getByRole('button', { name: '确认更改' }));
    await waitFor(() => expect(transport.requests.some((call) => (
      call.request.pathId === 'agent.extensions.apply'
      && typeof call.request.body === 'object'
      && call.request.body !== null
      && !Array.isArray(call.request.body)
      && call.request.body.confirmText === 'apply'
      && call.request.body.previewToken === 'preview-token'
    ))).toBe(true));

    expect(await screen.findByText('更改已应用')).toBeVisible();
    expect(screen.getByText('安装插件：Guided Plugin')).toBeVisible();
    expect(screen.getByText(/回执 plugin:install:test/)).toBeVisible();
    await user.click(screen.getByRole('button', { name: '知道了' }));
    expect(screen.queryByText('更改已应用')).not.toBeInTheDocument();
  });

  it('shows governed versions and lifecycle policies, and can toggle a hook', async () => {
    const user = userEvent.setup();
    const transport = renderPlugins();
    expect(screen.queryByText('对话复盘')).not.toBeInTheDocument();
    await user.click(await screen.findByRole('button', { name: '管理扩展与自动整理' }));
    expect(await screen.findByText('对话复盘')).toBeInTheDocument();
    expect(screen.getByText('v1.1.0 · 2 个版本')).toBeInTheDocument();
    expect(screen.getByText('为下一轮准备记忆建议')).toBeInTheDocument();
    await user.click(screen.getByRole('switch', { name: '任务完成：已启用' }));
    await waitFor(() => expect(transport.requests.some((call) => (
      call.request.pathId === 'agent.lifecycleHooks.update'
      && typeof call.request.body === 'object'
      && call.request.body !== null
      && !Array.isArray(call.request.body)
      && call.request.body.eventType === 'project_complete'
      && call.request.body.enabled === false
    ))).toBe(true));
  });

  it('states the lifecycle record boundary and lets the user reveal every older event', async () => {
    const user = userEvent.setup();
    renderPlugins({
      'agent.lifecycleHooks.get': {
        ok: true,
        policies: [],
        recentEvents: Array.from({ length: 11 }, (_, index) => ({
          eventId: `event-${index + 1}`,
          eventType: `custom_${index + 1}`,
          status: 'completed',
        })),
      },
    });

    await user.click(await screen.findByRole('button', { name: '管理扩展与自动整理' }));
    expect(await screen.findByText('最近 8 / 共 11 条')).toBeVisible();
    expect(screen.queryByText('custom_9')).not.toBeInTheDocument();

    await user.click(screen.getByText('查看其余 3 条记录'));
    expect(screen.getByText('custom_9')).toBeVisible();
    expect(screen.getByText('custom_11')).toBeVisible();
  });

  it('does not disguise plugin query failures as empty installed or proposal states', async () => {
    const user = userEvent.setup();
    renderPlugins({
      'agent.extensions.catalog': () => {
        throw new Error('catalog unavailable');
      },
    });

    await user.click(await screen.findByRole('button', { name: '管理扩展与自动整理' }));
    expect(await screen.findByText('读取失败')).toBeVisible();
    expect(screen.getByText('暂时无法读取这部分内容，请稍后重试。')).toBeVisible();
    expect(screen.queryByText('还没有受管插件')).not.toBeInTheDocument();
  });

  it('shows a recoverable Pi disconnect instead of an empty installed state', async () => {
    const user = userEvent.setup();
    renderPlugins({
      'agent.extensions.list': {
        schemaVersion: 'rag-ime.plugin-inventory.v1',
        ok: true,
        runtimeAvailable: false,
        items: [],
      },
    });

    await user.click(await screen.findByRole('button', { name: '管理扩展与自动整理' }));
    expect(await screen.findByText('Pi Runtime 暂时未连接')).toBeVisible();
    expect(screen.getByText('Pi 未连接')).toBeVisible();
    expect(screen.getByText(/不会再把断连伪装成“0 个已安装”/)).toBeVisible();
  });

  it('opens the exact installed Package when entered from a PAWOS Package App window', async () => {
    renderPlugins({
      'agent.extensions.list': {
        schemaVersion: 'rag-ime.plugin-inventory.v1',
        ok: true,
        runtimeAvailable: true,
        items: [{
          id: '@paw/pi-session-workflow',
          displayName: 'Session Workflow',
          version: '1.0.0',
          digest: 'a'.repeat(64),
          enabled: true,
          installed: true,
          rollbackAvailable: false,
          permissions: [],
          resources: { skills: ['skills/session-workflow/SKILL.md'] },
        }],
      },
    }, '/plugins?packageId=%40paw%2Fpi-session-workflow');

    const packageCard = await screen.findByRole('article', { name: 'Session Workflow Package' });
    expect(packageCard).toHaveAttribute('data-selected', 'true');
    expect(packageCard).toHaveTextContent('v1.0.0');
    expect(screen.getByRole('button', { name: '收起维护选项' })).toBeInTheDocument();
  });

  it('keeps an uninstalled Extension App on the existing guarded install action', async () => {
    const user = userEvent.setup();
    renderPlugins({
      'agent.extensions.catalog': {
        ok: true,
        items: [extensionCatalogItem({ installed: false, actionable: true })],
      },
    });

    await user.click(await screen.findByRole('button', { name: '管理扩展与自动整理' }));
    const row = document.querySelector('.plugin-catalog__row');
    expect(row).not.toBeNull();
    expect(row?.querySelector('[data-paw-app-icon="extension:zhanggui-wenshu"]')).toBeInTheDocument();
    expect(row).toHaveTextContent('PAWOS App');
    expect(within(row as HTMLElement).getByRole('button', { name: '查看安装内容' })).toBeEnabled();
    expect(within(row as HTMLElement).queryByRole('button', { name: /打开\s*掌柜问数/ })).not.toBeInTheDocument();
  });

  it('opens an enabled Extension App from both its catalog row and installed Package card', async () => {
    const user = userEvent.setup();
    renderPlugins({
      'agent.extensions.list': {
        schemaVersion: 'rag-ime.plugin-inventory.v1',
        ok: true,
        runtimeAvailable: true,
        items: [extensionInventoryItem()],
      },
      'agent.extensions.catalog': {
        ok: true,
        items: [extensionCatalogItem({ installed: true, enabled: true, actionable: true })],
      },
    });

    await user.click(await screen.findByRole('button', { name: '管理扩展与自动整理' }));
    const row = document.querySelector('.plugin-catalog__row');
    expect(row).not.toBeNull();
    expect(row?.querySelector('[data-paw-app-icon="extension:zhanggui-wenshu"]')).toBeInTheDocument();
    expect(row).toHaveTextContent('PAWOS App');
    await user.click(within(row as HTMLElement).getByRole('button', { name: /打开\s*掌柜问数/ }));
    expect(window.location.hash).toBe('#/extensions/zhanggui-wenshu');

    window.history.replaceState({}, '', '/');
    const card = await screen.findByRole('article', { name: '掌柜问数 Package' });
    expect(card.querySelector('[data-paw-app-icon="extension:zhanggui-wenshu"]')).toBeInTheDocument();
    expect(card).toHaveTextContent('PAWOS App');
    await user.click(within(card).getByRole('button', { name: /打开\s*掌柜问数/ }));
    expect(window.location.hash).toBe('#/extensions/zhanggui-wenshu');
    expect(within(card).getByRole('button', { name: '停用' })).toBeInTheDocument();
    expect(within(card).getByRole('button', { name: '卸载' })).toBeInTheDocument();
  });

  it('refreshes catalog, installed versions, proposals and lifecycle together', async () => {
    const user = userEvent.setup();
    const transport = renderPlugins();
    await user.click(await screen.findByRole('button', { name: '管理扩展与自动整理' }));
    await screen.findByText('对话复盘');
    const tracked: ControlPathId[] = [
      'agent.tools.list',
      'agent.extensions.list',
      'agent.extensions.catalog',
      'agent.extensions.proposals',
      'agent.lifecycleHooks.get',
    ];
    const before = new Map(tracked.map((pathId) => [
      pathId,
      transport.requests.filter((call) => call.request.pathId === pathId).length,
    ]));

    await user.click(screen.getByRole('button', { name: '刷新' }));

    await waitFor(() => {
      for (const pathId of tracked) {
        expect(transport.requests.filter((call) => call.request.pathId === pathId).length)
          .toBeGreaterThan(before.get(pathId) ?? 0);
      }
    });
  });

  it('surfaces lifecycle hook mutation failures without hiding the current policy', async () => {
    const user = userEvent.setup();
    renderPlugins({
      'agent.lifecycleHooks.update': () => {
        throw new Error('Hook 更新被拒绝');
      },
    });

    await user.click(await screen.findByRole('button', { name: '管理扩展与自动整理' }));
    await user.click(await screen.findByRole('switch', { name: '任务完成：已启用' }));
    expect(await screen.findByText('自动整理设置没有保存')).toBeVisible();
    expect(screen.getByText('Hook 更新被拒绝')).toBeVisible();
    expect(screen.getByText('为下一轮准备记忆建议')).toBeVisible();
  });

  it('shows the reviewed plugin version, capabilities and enabled state before apply', async () => {
    const user = userEvent.setup();
    renderPlugins({
      'agent.extensions.proposals': {
        ok: true,
        items: [{
          proposalId: 'proposal-disable',
          previewToken: 'preview-disable',
          payloadSha256: 'b'.repeat(64),
          summary: {
            action: 'disable',
            pluginId: 'session-review',
            displayName: 'Session Review',
            version: '1.1.0',
            permissions: ['session.read'],
            expectedEnabled: true,
            expectedActiveDigest: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
          },
        }],
      },
    });

    await user.click(await screen.findByRole('button', { name: '管理扩展与自动整理' }));
    await user.click(await screen.findByRole('button', { name: /对话复盘/ }));
    const approval = await screen.findByRole('region', { name: '待确认的插件更改' });
    const fact = (label: string) => within(approval).getByText(label).nextElementSibling as HTMLElement;
    expect(fact('版本')).toHaveTextContent('v1.1.0');
    expect(fact('需要的权限')).toHaveTextContent('读取对话内容');
    expect(fact('当前状态')).toHaveTextContent('已启用');
  });
  it('discards the old approval when a different lifecycle preview fails', async () => {
    const user = userEvent.setup();
    let previews = 0;
    const transport = renderPlugins({
      'agent.extensions.list': { ok: true, items: [{ id: 'session-review', displayName: 'Session Review', version: '1.0.0', enabled: true, installed: true }] },
      'agent.extensions.preview': () => {
        if (++previews > 1) throw new Error('卸载预览失败');
        return { ok: true, previewToken: 'old-disable', payloadSha256: 'a'.repeat(64), summary: { action: 'disable', pluginId: 'session-review' } };
      },
    }, '/plugins', true);
    const card = await screen.findByRole('article', { name: '对话复盘 Package' });
    await user.click(within(card).getByRole('button', { name: '停用' }));
    expect(await screen.findByRole('button', { name: '确认更改' })).toBeEnabled();
    await user.click(within(card).getByRole('button', { name: '卸载' }));
    expect(await screen.findByText('卸载预览失败')).toBeVisible();
    expect(screen.queryByRole('region', { name: '待确认的插件更改' })).not.toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.extensions.apply')).toBe(false);
  });

  it.each(['source', 'catalog', 'update'] as const)('retires old approval when %s preparation fails before preview', async (entry) => {
    const user = userEvent.setup();
    const transport = renderPlugins({
      'agent.extensions.list': { ok: true, items: [{ id: 'session-review', displayName: 'Session Review', version: '1.0.0', enabled: true, installed: true }] },
      ...(entry === 'update' ? { 'agent.extensions.catalog': { ok: true, items: [{ id: 'session-review', displayName: 'Session Review', latestVersion: '1.1.0', installed: true, updateAvailable: true, actionable: true }] } } : {}),
      'agent.extensions.validate': () => { throw new Error('来源准备失败'); },
    });
    await user.click(await screen.findByRole('button', { name: '管理扩展与自动整理' }));
    const card = await screen.findByRole('article', { name: '对话复盘 Package' });
    await user.click(within(card).getByRole('button', { name: '停用' }));
    expect(await screen.findByRole('button', { name: '确认更改' })).toBeEnabled();
    if (entry === 'source') {
      await user.type(screen.getByRole('textbox', { name: 'Pi Package 来源' }), 'npm:example-package');
      await user.click(screen.getByRole('button', { name: '检查并预览' }));
    } else if (entry === 'update') {
      await user.click(within(card).getByRole('button', { name: '更新到 v1.1.0' }));
    } else {
      await user.click(screen.getByRole('button', { name: '查看安装内容' }));
    }
    expect(await screen.findByText('来源准备失败')).toBeVisible();
    expect(screen.queryByRole('button', { name: '确认更改' })).not.toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.extensions.preview')).toHaveLength(1);
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.extensions.apply')).toBe(false);
  });

  it('does not let a proposal replace an in-flight lifecycle intent', async () => {
    const user = userEvent.setup();
    let finishPreview!: (value: unknown) => void;
    const pending = new Promise((resolve) => { finishPreview = resolve; });
    renderPlugins({
      'agent.extensions.list': { ok: true, items: [{ id: 'session-review', displayName: 'Session Review', version: '1.0.0', enabled: true, installed: true }] },
      'agent.extensions.proposals': { ok: true, items: [{ proposalId: 'other-proposal', previewToken: 'other-token', summary: { action: 'uninstall', pluginId: 'other-plugin', displayName: '另一个插件' } }] },
      'agent.extensions.preview': () => pending,
    });
    await user.click(await screen.findByRole('button', { name: '管理扩展与自动整理' }));
    await user.click(within(await screen.findByRole('article', { name: '对话复盘 Package' })).getByRole('button', { name: '停用' }));
    expect(screen.getByRole('button', { name: /另一个插件/ })).toBeDisabled();
    expect(screen.queryByRole('button', { name: '确认更改' })).not.toBeInTheDocument();
    await act(async () => { finishPreview({ ok: true, previewToken: 'disable-token', payloadSha256: 'a'.repeat(64), summary: { action: 'disable', pluginId: 'session-review' } }); });
    expect(await screen.findByRole('region', { name: '待确认的插件更改' })).toHaveTextContent('停用插件');
    expect(screen.getByRole('button', { name: /另一个插件/ })).toBeEnabled();
  });

  it.each([{ ok: false }, { ok: true }, null])('does not report an unconfirmed apply as success: %j', async (response) => {
    const user = userEvent.setup();
    const transport = renderPlugins({
      'agent.extensions.list': { ok: true, items: [{ id: 'session-review', displayName: 'Session Review', version: '1.0.0', enabled: true, installed: true }] },
      'agent.extensions.apply': () => response,
    }, '/plugins', true);
    const card = await screen.findByRole('article', { name: '对话复盘 Package' });
    await user.click(within(card).getByRole('button', { name: '停用' }));
    await user.click(await screen.findByRole('button', { name: '确认更改' }));
    expect(await screen.findByText(/未收到有效的更改回执/)).toBeVisible();
    expect(screen.queryByText('更改已应用')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '确认更改' })).not.toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.extensions.apply')).toHaveLength(1);
  });

  it('clears a consumed approval after a lost apply response without retrying', async () => {
    const user = userEvent.setup();
    const transport = renderPlugins({
      'agent.extensions.list': { ok: true, items: [{ id: 'session-review', displayName: 'Session Review', version: '1.0.0', enabled: true, installed: true }] },
      'agent.extensions.apply': () => { throw new Error('连接已断开'); },
    }, '/plugins', true);
    await user.click(within(await screen.findByRole('article', { name: '对话复盘 Package' })).getByRole('button', { name: '停用' }));
    await user.click(await screen.findByRole('button', { name: '确认更改' }));
    expect(await screen.findByText(/请先刷新安装状态/)).toHaveTextContent('连接已断开');
    expect(screen.queryByRole('button', { name: '确认更改' })).not.toBeInTheDocument();
    expect(screen.queryByText('更改已应用')).not.toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.extensions.apply')).toHaveLength(1);
  });

  it('requires explicit confirmation before disabling or rolling back an installed Package', async () => {
    const user = userEvent.setup();
    const transport = createPreviewTransport();
    renderPluginsWithTransport(transport);

    await user.click(await screen.findByRole('button', { name: '管理扩展与自动整理' }));
    await screen.findByText('时间线检查');
    expect(screen.getByText('可恢复到 v0.9.0')).toBeInTheDocument();
    const installedPackage = screen.getByRole('article', { name: '时间线检查 Package' });
    expect(within(installedPackage).getByText('1 项资源')).toBeInTheDocument();

    await user.click(within(installedPackage).getByRole('button', { name: '停用' }));
    expect(await screen.findByText('等待你的批准')).toBeVisible();
    expect(transport.requests.filter((call) => call.request.pathId === 'agent.extensions.apply')).toHaveLength(0);
    await user.click(screen.getByRole('button', { name: '确认更改' }));
    await waitFor(() => expect(transport.requests.filter((call) => call.request.pathId === 'agent.extensions.apply')).toHaveLength(1));

    const rollback = within(screen.getByRole('article', { name: '时间线检查 Package' })).getByRole('button', { name: '恢复上一版本' });
    expect(rollback).toBeEnabled();
    await user.click(rollback);
    expect(await screen.findByText('等待你的批准')).toBeVisible();
    expect(transport.requests.filter((call) => call.request.pathId === 'agent.extensions.apply')).toHaveLength(1);
    await user.click(screen.getByRole('button', { name: '确认更改' }));
    await waitFor(() => expect(transport.requests.filter((call) => call.request.pathId === 'agent.extensions.apply')).toHaveLength(2));

    expect(await screen.findByText('v0.9.0')).toBeVisible();
    const restoredPackage = within(screen.getByRole('article', { name: '时间线检查 Package' }));
    expect(restoredPackage.getByRole('button', { name: '恢复上一版本' })).toBeDisabled();
    expect(restoredPackage.getByText('没有可恢复的历史版本')).toBeInTheDocument();
  });

  it('previews uninstall in App Center, states retained data, and removes only after confirmation', async () => {
    const user = userEvent.setup();
    const transport = createPreviewTransport();
    renderPluginsWithTransport(transport);

    await user.click(await screen.findByRole('button', { name: '管理扩展与自动整理' }));
    await screen.findByText('时间线检查');

    await user.click(within(screen.getByRole('article', { name: '时间线检查 Package' })).getByRole('button', { name: '卸载' }));

    expect(await screen.findByText('等待你的批准')).toBeVisible();
    expect(screen.getByText(/不会删除项目文件、对话、WorkDocument 或个人数据/)).toBeVisible();
    expect(transport.requests.some((call) => call.request.pathId === 'agent.extensions.apply')).toBe(false);

    await user.click(screen.getByRole('button', { name: '确认更改' }));
    await waitFor(() => expect(transport.requests.filter((call) => (
      call.request.pathId === 'agent.extensions.apply'
    ))).toHaveLength(1));

    expect(screen.queryByRole('article', { name: '时间线检查 Package' })).not.toBeInTheDocument();
    expect(screen.getByText('卸载插件：时间线检查')).toBeVisible();
    expect(screen.getByText(/回执 plugin:uninstall:preview/)).toBeVisible();
    // Other installed Packages are independent of this uninstall.
    expect(screen.getAllByRole('article').some((item) => item.getAttribute('aria-label')?.endsWith(' Package'))).toBe(true);
  });

  it('uses the same skill purposes as scene loading while preserving original identity and body', async () => {
    const user = userEvent.setup();
    const transport = renderPlugins({
      'agent.extensions.skills.list': { schemaVersion: 'rag-ime.skill-inventory.v1', ok: true, runtimeAvailable: true, items: [
        { skillId: 'skill:debug:exact', name: 'systematic-debugging', description: 'Original diagnostic guidance.', sourceKind: 'bundled',
          installed: true, enabled: null, management: 'inspect_only',
          managementReason: 'This Skill is supplied by Pi and has no independent enable switch.', actions: [] },
        { skillId: 'project:custom', name: 'Project Guide', description: 'Author supplied content.', sourceKind: 'project', installed: true, enabled: null, management: 'inspect_only' },
        { skillId: 'project:debug', name: 'systematic-debugging', description: 'Custom project instructions.', sourceKind: 'project', installed: true, enabled: null, management: 'inspect_only' },
      ] },
      'agent.extensions.skills.get': { ok: true, item: { body: '# Original Skill body' } },
    }, '/plugins?view=skills');
    await user.type(await screen.findByRole('textbox', { name: '搜索' }), '故障');
    const list = await screen.findByRole('group', { name: 'Skill 列表' });
    await user.click(within(list).getByRole('button', { name: /故障定位/ }));
    const detail = screen.getByRole('complementary', { name: 'Skill 详情' });
    expect(within(detail).getByRole('heading', { name: '故障定位' })).toBeVisible();
    expect(detail).toHaveTextContent('先复现并定位故障原因，再验证修复');
    expect(detail).toHaveTextContent('systematic-debugging');
    expect(detail).not.toHaveTextContent('This Skill is supplied by Pi');
    expect(detail).not.toHaveTextContent('随产品提供 · 随产品提供');
    expect(await within(detail).findByText('# Original Skill body')).toBeVisible();
    expect(transport.requests.find(({ request }) => request.pathId === 'agent.extensions.skills.get')?.request.query).toEqual({ skillId: 'skill:debug:exact' });
    await user.click(within(detail).getByText('查看原始用途说明'));
    expect(detail).toHaveTextContent('Original diagnostic guidance.');
    await user.clear(screen.getByRole('textbox', { name: '搜索' }));
    await user.type(screen.getByRole('textbox', { name: '搜索' }), 'systematic-debugging');
    expect(within(list).getByRole('button', { name: /故障定位/ })).toBeVisible();
    expect(within(list).getByRole('button', { name: /systematic-debugging/ })).toHaveTextContent('Custom project instructions.');
    await user.click(within(detail).getByRole('button', { name: '设置场景加载' }));
    expect(screen.getByTestId('test-location')).toHaveTextContent('/plugins?view=scenes');
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.configuration.update')).toBe(false);
  });

  it('browses Skills by source, reads bounded detail, and scopes Package actions', async () => {
    const user = userEvent.setup();
    const packageSkill = {
      skillId: 'package-skill',
      name: 'Package Skill',
      description: '来自已安装 Package 的 Skill',
      sourceKind: 'package',
      packageId: 'skills-package',
      packageVersion: '1.2.0',
      resourcePath: 'skills/package-skill/SKILL.md',
      enabled: true,
      installed: true,
      installState: 'enabled',
      digest: `sha256:${'1'.repeat(64)}`,
      contentRevision: `sha256:${'1'.repeat(64)}`,
      sizeBytes: 240,
      management: 'package',
      managementReason: 'Skill 生命周期属于整个 Package。',
      actions: ['enable', 'disable', 'update', 'uninstall'],
    };
    const projectSkill = {
      skillId: 'project-skill',
      name: 'Project Skill',
      description: '来自当前项目的 Skill',
      sourceKind: 'project',
      resourcePath: 'skills/project-skill/SKILL.md',
      enabled: null,
      installed: true,
      installState: 'project',
      digest: `sha256:${'2'.repeat(64)}`,
      contentRevision: `sha256:${'2'.repeat(64)}`,
      sizeBytes: 180,
      management: 'inspect_only',
      managementReason: '项目 Skill 没有独立 Package 生命周期。',
      actions: [],
    };
    const transport = renderPlugins({
      'agent.extensions.list': {
        schemaVersion: 'rag-ime.plugin-inventory.v1',
        ok: true,
        runtimeAvailable: true,
        items: [{ id: 'skills-package', version: '1.2.0', enabled: true, installed: true }],
      },
      'agent.extensions.skills.list': {
        schemaVersion: 'rag-ime.skill-inventory.v1',
        ok: true,
        runtimeAvailable: true,
        revision: `sha256:${'a'.repeat(64)}`,
        items: [packageSkill, projectSkill],
      },
      'agent.extensions.skills.get': {
        schemaVersion: 'rag-ime.skill-detail.v1',
        ok: true,
        revision: packageSkill.contentRevision,
        item: {
          ...packageSkill,
          body: '# Package Skill\n\n只读正文来自服务器。',
          bodyBytes: 48,
          contentBytes: 48,
          bodyTruncated: false,
        },
      },
      'agent.extensions.preview': {
        ok: true,
        previewToken: 'preview:disable:skills-package',
        payloadSha256: 'b'.repeat(64),
        summary: {
          action: 'disable',
          pluginId: 'skills-package',
          displayName: 'Package Skill',
          expectedEnabled: false,
        },
      },
    }, '/plugins?view=skills');

    expect(await screen.findByRole('heading', { name: 'Skills 管理', level: 1 })).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: /Package Skill/ })).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: /Project Skill/ })).toBeInTheDocument();
    await user.click(screen.getByRole('combobox', { name: '来源' }));
    await user.click(screen.getByRole('option', { name: '当前项目' }));
    expect(screen.queryByRole('button', { name: /Package Skill/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Project Skill/ })).toBeInTheDocument();
    await user.click(screen.getByRole('combobox', { name: '来源' }));
    await user.click(screen.getByRole('option', { name: '全部来源' }));
    await user.click(screen.getByRole('button', { name: /Package Skill/ }));
    expect(await screen.findByText(/只读正文来自服务器。/)).toBeVisible();
    expect(screen.getByText('Package 范围管理')).toBeVisible();
    await user.click(screen.getByRole('button', { name: '停用整个 Package' }));
    await waitFor(() => {
      const request = transport.requests.find((candidate) => (
        candidate.request.pathId === 'agent.extensions.preview'
      ));
      expect(request?.request.body).toMatchObject({
        action: 'disable',
        pluginId: 'skills-package',
      });
    });
    expect(screen.getByRole('region', { name: '待确认的插件更改' })).toBeVisible();
    expect(screen.getByRole('button', { name: '确认更改' })).toBeVisible();
  });

});

function renderPlugins(
  overrides: Partial<Record<ControlPathId, MockRouteHandler>> = {},
  initialEntry = '/plugins',
  pawOs = false,
  active = true,
) {
  const transport = new MockControlTransport({
    pickedFiles: [{
      id: 'plugin-source-1',
      name: 'guided-plugin',
      mimeType: 'application/octet-stream',
      byteSize: 0,
      path: '/trusted/guided-plugin',
    }],
    routes: {
      'agent.tools.list': capabilityCatalog(toolItems()),
      'agent.configuration.get': capabilityDefaults(),
      'agent.configuration.update': { ok: true },
      'agent.extensions.list': { ok: true, items: [] },
      'agent.extensions.catalog': {
        ok: true,
        items: [{
          id: 'session-review',
          displayName: 'Session Review',
          description: '基于事实审阅会话结果',
          publisher: 'Personal Agent Workbench',
          source: { kind: 'bundled', label: 'Product bundle' },
          permissions: ['session.read'],
          security: { notes: '只读会话权限' },
          versions: [{ version: '1.1.0' }, { version: '1.0.0' }],
          latestVersion: '1.1.0',
          installed: false,
          updateAvailable: false,
          actionable: true,
        }],
      },
      'agent.extensions.proposals': { ok: true, items: [] },
      'agent.lifecycleHooks.get': {
        ok: true,
        policies: [{ eventType: 'project_complete', enabled: true, action: 'memory_review_suggestion', tokenLimit: 256, cooldownSeconds: 300 }],
        recentEvents: [],
      },
      'agent.lifecycleHooks.update': { ok: true },
      'agent.extensions.validate': {
        ok: true,
        validationToken: 'validation-token',
        extension: { id: 'guided-plugin', displayName: 'Guided Plugin', version: '1.0.0', totalBytes: 128 },
      },
      'agent.extensions.preview': {
        ok: true,
        previewToken: 'preview-token',
        payloadSha256: 'a'.repeat(64),
        summary: { action: 'install', pluginId: 'guided-plugin', displayName: 'Guided Plugin' },
      },
      'agent.extensions.apply': { ok: true, receipt: { receiptId: 'plugin:install:test' } },
      ...overrides,
    },
  });
  renderPluginsWithTransport(transport, initialEntry, pawOs, active);
  return transport;
}

function renderPluginsWithTransport(
  transport: ControlTransport,
  initialEntry = '/plugins',
  pawOs = false,
  active = true,
): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const feature = <QueryClientProvider client={client}><PluginsFeature /></QueryClientProvider>;
  render(<MemoryRouter initialEntries={[initialEntry]}><LocationProbe /><TooltipProvider delayDuration={0}><ControlTransportProvider transport={transport}>{pawOs ? <PawOsAppSurfaceProvider active={active} appId="app-center" height={720} width={1_080}>{feature}</PawOsAppSurfaceProvider> : feature}</ControlTransportProvider></TooltipProvider></MemoryRouter>);
}

function LocationProbe() {
  const location = useLocation();
  return <span data-testid="test-location" hidden>{`${location.pathname}${location.search}`}</span>;
}

function extensionCatalogItem(overrides: Record<string, unknown> = {}) {
  const extension = pawExtensionApps.find((app) => app.packageId === '@paw/zhanggui-wenshu')!;
  return {
    id: '@paw/zhanggui-wenshu',
    displayName: '掌柜问数',
    description: '受控的垂直问数工作台',
    publisher: 'Personal Agent Workbench',
    source: { kind: 'bundled', label: 'Product bundle' },
    permissions: ['sandbox.run'],
    capabilities: [`pawos.extension.binding.${extension.bindingSha256.slice(0, 40)}`],
    security: { notes: 'Host 拥有沙盒边界，Package 不直接写入生产数据。' },
    versions: [{ version: extension.version }],
    version: extension.version,
    latestVersion: extension.version,
    installed: false,
    enabled: false,
    extensionApp: {
      id: extension.id,
      packageId: extension.packageId,
      version: extension.version,
      bindingSha256: extension.bindingSha256,
      bindingCapability: `pawos.extension.binding.${extension.bindingSha256.slice(0, 40)}`,
      skillRef: extension.skillRef,
      skillSha256: extension.skillSha256,
      verticalSuiteId: extension.verticalSuiteId,
      verticalSuiteRevision: extension.verticalSuiteRevision,
      sandbox: extension.sandbox,
    },
    updateAvailable: false,
    actionable: true,
    ...overrides,
  };
}

function extensionInventoryItem(overrides: Record<string, unknown> = {}) {
  const extension = pawExtensionApps.find((app) => app.packageId === '@paw/zhanggui-wenshu')!;
  return {
    id: '@paw/zhanggui-wenshu',
    displayName: '掌柜问数',
    version: extension.version,
    digest: 'c'.repeat(64),
    enabled: true,
    installed: true,
    rollbackAvailable: false,
    permissions: ['sandbox.run'],
    capabilities: [`pawos.extension.binding.${extension.bindingSha256.slice(0, 40)}`],
    resources: {},
    extensionApp: {
      id: extension.id,
      packageId: extension.packageId,
      version: extension.version,
      bindingSha256: extension.bindingSha256,
      bindingCapability: `pawos.extension.binding.${extension.bindingSha256.slice(0, 40)}`,
      skillRef: extension.skillRef,
      skillSha256: extension.skillSha256,
      verticalSuiteId: extension.verticalSuiteId,
      verticalSuiteRevision: extension.verticalSuiteRevision,
      sandbox: extension.sandbox,
    },
    ...overrides,
  };
}

function toolItems() {
  return [
    tool({ id: 'memory', displayName: '记忆与工具书', description: '搜索与维护长期记忆', domain: 'memory', riskLevel: 'R1', operations: ['search', 'maintenance_apply'], sessionModes: ['assistant', 'coordinator'] }),
    tool({ id: 'configuration', displayName: '历史与配置', description: '查看历史并恢复便携备份', domain: 'configuration', riskLevel: 'R3', operations: ['history', 'restore_apply'], sessionModes: ['assistant', 'coordinator'], privatePath: '/api/private/tools' }),
    tool({ id: 'voice', displayName: '语音输入', description: '查看语音状态与服务连接', domain: 'voice', riskLevel: 'R1', operations: ['provider_status'], sessionModes: ['assistant', 'coordinator'] }),
    tool({ id: 'workspace_read', displayName: '工作区读取', description: '读取已授权工作区中的文本', domain: 'workspace', riskLevel: 'R0', operations: ['read'], sessionModes: ['coordinator'], availability: 'offline' }),
  ];
}

function tool(overrides: Record<string, unknown>) {
  const id = String(overrides.id);
  const risk = String(overrides.riskLevel ?? 'R0');
  const status = String(overrides.availability ?? 'online');
  return {
    category: overrides.domain,
    operationRisks: {},
    resultPresentation: 'tool_result',
    availability: status,
    version: '1',
    ...overrides,
    canonicalId: `tool:${id}`,
    kind: 'tool' as const,
    source: { kind: 'product', label: 'Personal Agent Workbench' },
    status,
    risk,
    requiredPermissions: [
      ...(risk === 'R0' ? [] : ['native_approval']),
      ...(id.startsWith('workspace_') ? ['workspace_scope'] : []),
    ],
    authorization: { state: 'not_applicable' as const, reason: 'session_context_required' },
    disclosure: {
      preference: 'inherit' as const,
      effective: 'enabled' as 'enabled' | 'disabled',
      state: 'disclosed' as 'disclosed' | 'hidden',
      reason: 'inherited_built_in_default',
    },
    effectiveScope: 'built_in_default' as const,
    reasons: ['inherited_built_in_default', 'tool_not_authorized_by_existing_session_policy'],
    revision: 'tool-spec:1',
    effectiveAtMs: 1,
  };
}

function capabilityCatalog(
  items: ReturnType<typeof toolItems>,
  projectScope: Record<string, unknown> = {
    supported: false,
    identityKind: 'none',
    reason: 'stable_project_identity_unavailable',
  },
  sessionId = '',
) {
  return {
    schemaVersion: 'rag-ime.capability-catalog.v1',
    ok: true,
    revision: `sha256:${'a'.repeat(64)}`,
    effectiveAtMs: 1,
    projectScope,
    ...(sessionId ? {
      sessionPolicy: {
        sessionId,
        policyRevision: 1,
        disclosurePreferences: {
          globalDefault: {},
          projectDefault: { 'tool:memory': 'enabled' },
          session: {},
          effective: Object.fromEntries(items.map((item) => [item.canonicalId, item.disclosure.effective])),
        },
        effectiveAtMs: 1,
      },
    } : {}),
    items,
  };
}

function capabilityDefaults(
  projectPreferences: Record<string, Record<string, string>> = {},
) {
  return {
    ok: true,
    configuration: {
      revision: 1,
      configuration: {
        sessionDefaults: { capabilityDisclosurePreferences: {} },
        capabilityDisclosure: { projectPreferences },
      },
    },
  };
}
