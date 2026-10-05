import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import type { SessionSummary } from '@/features/agent/types';
import type { AgentPersonaV1 } from '@/contracts/generated/agent-persona.v1';
import type { PiModelOption } from '@/features/agent/model-catalog-options';
import type { AgentImagePasteOptions, ControlRequest, ControlTransport, PickedFile } from '@/platform/transport';
import type { RoomSummary } from '@/features/rooms/room-types';
import { agentSessionAddress, selectAgentProjection, useAgentLiveStore } from '@/features/agent/state/live-store';
import { ControlTransportHttpError } from '@/platform/http-transport';
import { MockControlTransport, type MockRouteHandler } from '@/test/mock-transport';
import agentNextCss from '../styles/paw-os-agent-next.css?raw';
import { PawAgentHome } from './PawAgentHome';
import { warmAgentWorkspace } from './agent-workspace-loader';

vi.mock('./agent-workspace-loader', async importOriginal => ({
  ...await importOriginal<typeof import('./agent-workspace-loader')>(),
  warmAgentWorkspace: vi.fn(),
}));

import { readRoomEntryMode } from '@/features/semantic-workspace/room-entry-mode';
import { createAgentModeStore } from '@/features/semantic-workspace/agent-mode-store';

afterEach(() => {
  cleanup();
  useAgentLiveStore.setState({ projections: {} });
});

describe('PAWOS Agent Home 首屏合同', () => {
  it('keeps Durable unavailable until the owned Host advertises it', () => {
    renderHome();
    expect(screen.getByRole('option', { name: 'Pi Durable（实验）' })).toBeDisabled();
    expect(screen.getByRole('combobox', { name: '会话执行方式' })).toHaveValue('classic');
  });

  it('creates an explicitly selected Durable Session and retains the server engine in admission', async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    const { transport } = renderHome({ durableAvailable: true, onCreated,
      createRoute: (request: ControlRequest) => ({ ok: true, session: {
        id: 'session-durable', title: '持续工作', runtimeEngine: (request.body as Record<string, unknown>).runtimeEngine,
      } }),
    });
    await user.selectOptions(screen.getByRole('combobox', { name: '会话执行方式' }), 'durable');
    await user.type(screen.getByRole('textbox', { name: '描述你想完成的工作' }), '继续完善这个项目');
    await user.click(screen.getByRole('button', { name: '开始 Session' }));
    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.prompt')).toBe(true));
    expect(transport.requests.find(({ request }) => request.pathId === 'agent.sessions.create')?.request.body).toMatchObject({ runtimeEngine: 'durable' });
    expect(onCreated).toHaveBeenCalledWith({ kind: 'session', id: 'session-durable' }, expect.objectContaining({ runtimeEngine: 'durable' }));
  });

  it('preserves pasted attachments and the draft while blocking unsupported Durable submission', async () => {
    const user = userEvent.setup();
    const imagePaste = vi.fn(() => []);
    const { transport } = renderHome({ durableAvailable: true, imagePaste });
    const input = screen.getByRole('textbox', { name: '描述你想完成的工作' });
    await user.type(input, '查看这个图片');
    fireEvent.paste(input, { clipboardData: { files: [new File(['image'], 'diagram.png', { type: 'image/png' })], items: [] } });
    await user.selectOptions(screen.getByRole('combobox', { name: '会话执行方式' }), 'durable');
    await user.click(screen.getByRole('button', { name: '开始 Session' }));
    expect(await screen.findByText(/Pi Durable 暂不支持附件/)).toBeVisible();
    expect(input).toHaveValue('查看这个图片');
    expect(screen.getByRole('button', { name: '移除 diagram.png' })).toBeVisible();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.sessions.create')).toBe(false);
    expect(imagePaste).not.toHaveBeenCalled();
  });

  it('does not transfer a Durable selection into Room creation', async () => {
    const user = userEvent.setup();
    const { transport } = renderHome({ durableAvailable: true, personas: [persona('planner', '规划者'), persona('builder', '执行者')] });
    await user.selectOptions(screen.getByRole('combobox', { name: '会话执行方式' }), 'durable');
    await user.click(screen.getByRole('radio', { name: 'Room' }));
    expect(screen.queryByRole('combobox', { name: '会话执行方式' })).not.toBeInTheDocument();
    await user.type(screen.getByRole('textbox', { name: '描述你想完成的工作' }), '协作检查项目');
    await user.click(screen.getByRole('button', { name: '开始 Room' }));
    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'agent.room.message')).toBe(true));
    expect(transport.requests.find(({ request }) => request.pathId === 'agent.rooms.create')?.request.body).not.toHaveProperty('runtimeEngine');
  });

  it('warms only a precisely targeted recent card before navigation', async () => {
    vi.mocked(warmAgentWorkspace).mockClear();
    const { transport } = renderHome();
    const target = await screen.findByRole('button', { name: /迁移作战室/ });
    fireEvent.pointerEnter(target);
    fireEvent.focus(target);
    fireEvent.pointerDown(target);
    expect(vi.mocked(warmAgentWorkspace).mock.calls).toEqual([['room'], ['room'], ['room']]);
    expect(transport.requests.some(({ request }) => ['agent.runtime.ensure', 'agent.room.message', 'agent.session.prompt'].includes(request.pathId))).toBe(false);
  });

  it('warms the exact Room entry from the default Jev continuity surface without starting work', async () => {
    vi.mocked(warmAgentWorkspace).mockClear();
    const facts = { key: `room:${room().id}`, title: 'Jev Room 精确记录', revision: 'v1', observedAtMs: 1000,
      running: false, goal: null, requests: [], candidates: [], decisions: [], blockers: [], sources: [], missing: [],
      pendingDecisions: [], deliveries: [], organization: { pinned: false, placement: 'desk' }, executionAllowed: true, contextPack: {} };
    const { transport } = renderHome({ interfaceMode: 'jev', continuityRoute: { ok: true, items: [facts], failures: [] } });
    const target = await screen.findByRole('button', { name: /^Jev Room 精确记录/ });
    fireEvent.pointerEnter(target);
    fireEvent.focus(target);
    fireEvent.pointerDown(target);
    expect(vi.mocked(warmAgentWorkspace).mock.calls).toEqual([['room'], ['room'], ['room']]);
    expect(transport.requests.some(({ request }) => ['agent.continuity.suggest', 'agent.continuity.resume', 'agent.jev.command', 'agent.runtime.ensure'].includes(request.pathId))).toBe(false);
  });

  it('starts the default Jev mode with three partners and its real graph command, preserving the authorization policy', async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    const { transport } = renderHome({ interfaceMode: createAgentModeStore().getSnapshot(), onCreated,
      personas: [persona('planner', '规划者'), persona('builder', '执行者'), persona('reviewer', '核对者')] });
    await user.type(screen.getByRole('textbox', { name: '描述你想完成的工作' }), '分析依赖并完成改动');
    await user.click(screen.getByRole('button', { name: '开始 Jev 任务' }));
    await waitFor(() => expect(transport.requests.some(call => call.request.pathId === 'agent.jev.command')).toBe(true));
    const container = transport.requests.find(call => call.request.pathId === 'agent.rooms.create')?.request.body as Record<string, unknown>;
    expect(container.participants).toHaveLength(3);
    expect(container.permissionPolicy).toMatchObject({ room: { executionMode: 'full_trust' } });
    expect(transport.requests.find(call => call.request.pathId === 'agent.jev.command')?.request.body).toMatchObject({ action: 'create', message: '分析依赖并完成改动', modelRouting: 'balanced', toolApprovalMode: 'dispatch', verificationMode: 'auto', executionApproval: true });
    expect(transport.requests.some(call => call.request.pathId === 'agent.room.message' || call.request.pathId === 'agent.session.prompt')).toBe(false);
    expect(onCreated).toHaveBeenCalledWith({ kind: 'room', id: 'room-created' }, undefined, expect.objectContaining({ id: 'room-created' }));
    expect(readRoomEntryMode(transport, 'room-created')).toBe('jev');
  });
  it('sends the chosen review policy from the Jev Home menu', async () => {
    const user = userEvent.setup();
    const { transport } = renderHome({ interfaceMode: createAgentModeStore().getSnapshot(),
      personas: [persona('planner', '规划者'), persona('builder', '执行者')] });
    await user.click(screen.getByRole('button', { name: 'Jev 模型与工具设置' }));
    const menu = screen.getByRole('menu');
    expect(within(menu).getByRole('menuitemradio', { name: '由 Jev 按任务与证据决定' })).toHaveAttribute('aria-checked', 'true');
    expect(menu).toHaveTextContent('自动模式会直接接受证据，或按需要启动 Pi 复核');
    await user.click(within(menu).getByRole('menuitemradio', { name: '始终由其他伙伴复核' }));
    await user.type(screen.getByRole('textbox', { name: '描述你想完成的工作' }), '核对一项简单任务');
    await user.click(screen.getByRole('button', { name: '开始 Jev 任务' }));
    await waitFor(() => expect(transport.requests.some(call => call.request.pathId === 'agent.jev.command')).toBe(true));
    expect(transport.requests.find(call => call.request.pathId === 'agent.jev.command')?.request.body).toMatchObject({ action: 'create', verificationMode: 'independent' });
  });
  it('allows an ordinary Session from the Jev home with its model control and unchanged draft', async () => {
    const user = userEvent.setup();
    const { transport } = renderHome({ interfaceMode: createAgentModeStore().getSnapshot(), models: [model('gpt-5.6-luna', 'GPT-5.6 Luna')] });
    expect(screen.getByRole('radio', { name: 'Room' })).toBeChecked();
    const input = screen.getByRole('textbox', { name: '描述你想完成的工作' });
    await user.type(input, '继续一个普通对话');
    await user.click(screen.getByRole('radio', { name: 'Session' }));
    expect(input).toHaveValue('继续一个普通对话');
    expect(screen.getByRole('button', { name: /模型与推理：/ })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Jev 模型与工具设置' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '开始 Session' }));
    await waitFor(() => expect(transport.requests.some(call => call.request.pathId === 'agent.session.prompt')).toBe(true));
    expect(transport.requests.some(call => call.request.pathId === 'agent.rooms.create' || call.request.pathId === 'agent.jev.command')).toBe(false);
  });
  it('completes the new-work composer and 继续工作 on one fixed surface without a galaxy landing section', async () => {
    const { container } = renderHome();

    expect(await screen.findByRole('heading', { name: '今天想完成什么？' })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '描述你想完成的工作' })).toBeInTheDocument();

    const recent = (screen.getByRole('heading', { name: '继续工作' })).parentElement!;
    expect(within(recent).getByRole('button', { name: /发布检查/ })).toBeInTheDocument();
    expect(within(recent).getByRole('button', { name: /迁移作战室/ })).toBeInTheDocument();

    // 首屏之外没有第二段落地页内容：Room 星系板块已被砍掉。
    expect(screen.queryByText('Room 星系')).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Room 星系' })).not.toBeInTheDocument();

    // 继续工作挂在弹性滚动区上，是首屏内唯一的内部滚动面。
    expect(container.querySelector('.an-home-recents .an-recent-list')).not.toBeNull();
  });

  it('warms only the visible recent projections without opening Runtime streams or a heavy catalog', async () => {
    const { transport } = renderHome();
    await waitFor(() => expect(transport.requests.some(({ request }) => (
      request.pathId === 'agent.session.snapshot'
      || request.pathId === 'agent.room.conversationSnapshot'
    ))).toBe(true));
    const warmupReads = transport.requests.filter(({ request }) => (
      request.pathId === 'agent.session.snapshot'
      || request.pathId === 'agent.room.conversationSnapshot'
    ));
    expect(warmupReads.length).toBeGreaterThan(0);
    expect(warmupReads.every(({ request }) => (
      request.pathId === 'agent.room.conversationSnapshot'
      || request.query?.view === 'recent'
    ))).toBe(true);
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.models')).toBe(false);
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.snapshot' && request.query?.view !== 'recent')).toBe(false);
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.runtime.ensure')).toBe(false);
    expect(transport.subscriptionCalls).toHaveLength(0);
  });

  it('owns its viewport like a desktop app: the page never scrolls, only the recent list does', () => {
    // 表面本身钉死在窗口高度上，禁止整页往下翻。
    expect(agentNextCss).toMatch(/\.an-home\s*\{[^}]*height:\s*100%;[^}]*overflow:\s*hidden;/s);
    // 继续工作列表是唯一的内部滚动区。
    expect(agentNextCss).toMatch(/\.an-home-recents \.an-recent-list\s*\{[^}]*overflow:\s*hidden auto;/s);
    // 页脚是钉在底部的状态条，不是文章末尾。
    expect(agentNextCss).toMatch(/\.an-home-foot\s*\{[^}]*margin-top:\s*auto;/s);
  });

  it('changes work mode with arrow keys without losing the draft or starting work', async () => {
    const user = userEvent.setup();
    const { transport } = renderHome();
    const composer = screen.getByRole('textbox', { name: '描述你想完成的工作' });
    await user.type(composer, '先保留这个目标');
    const session = screen.getByRole('radio', { name: 'Session' });
    const room = screen.getByRole('radio', { name: 'Room' });
    session.focus();

    await user.keyboard('{ArrowRight}');
    expect(room).toHaveFocus();
    expect(room).toBeChecked();
    expect(session).toHaveAttribute('tabindex', '-1');
    expect(composer).toHaveValue('先保留这个目标');
    await user.keyboard('{Home}');
    expect(session).toHaveFocus();
    expect(session).toBeChecked();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.sessions.create' || request.pathId === 'agent.rooms.create')).toBe(false);
  });

  it('opens permission and project menus outside the clipped Home surface and restores keyboard focus', async () => {
    const user = userEvent.setup();
    renderHome();
    const permission = await screen.findByRole('button', { name: /权限 · 完全访问/ });
    permission.focus();
    await user.keyboard('{Enter}');
    const permissions = await screen.findByRole('menu');
    expect(permissions.closest('.an-home')).toBeNull();
    await user.keyboard('{End}');
    expect(within(permissions).getByRole('menuitemradio', { name: /^全自动/ })).toHaveFocus();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(permission).toHaveFocus());

    const project = screen.getByRole('button', { name: /起始项目/ });
    await user.click(project);
    expect(screen.getByRole('menu').closest('.an-home')).toBeNull();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(project).toHaveFocus());

    await user.click(screen.getByRole('radio', { name: 'Room' }));
    const roomPermission = screen.getByRole('button', { name: /权限/ });
    await user.click(roomPermission);
    const policy = screen.getByRole('dialog', { name: 'Room 三层权限' });
    expect(policy.closest('.an-home')).toBeNull();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(roomPermission).toHaveFocus());
  });

  it('collapses home composer chips to semantic marks before words vanish from the accessibility tree', async () => {
    renderHome();

    expect(await screen.findByRole('button', { name: /权限 · 完全访问/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /起始项目 · work\/paw|起始项目（可选）/ })).toBeInTheDocument();

    // The toolbar is a named container; narrow windows shed detail then labels.
    expect(agentNextCss).toMatch(/container:\s*an-home-composer\s*\/\s*inline-size/);
    expect(agentNextCss).toMatch(/@container an-home-composer \(max-width: 560px\)/);
    expect(agentNextCss).toMatch(/@container an-home-composer \(max-width: 420px\)/);
    expect(agentNextCss).toMatch(/\.an-chip-text/);
  });

  it('uses the shared compact model control, with reasoning visible before the searchable catalog', async () => {
    const user = userEvent.setup();
    renderHome({ modelReference: 'gpt/gpt-5.6-luna', models: [model('gpt-5.6-luna', 'GPT-5.6 Luna')] });
    const trigger = await screen.findByRole('button', { name: /模型与推理：GPT-5.6 Luna.*高/ });
    await user.click(trigger);
    let picker = screen.getByRole('dialog', { name: '选择模型与推理强度' });
    expect(within(picker).queryByRole('listbox')).not.toBeInTheDocument();
    await user.click(within(picker).getByRole('radio', { name: '中' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    const updated = screen.getByRole('button', { name: /模型与推理：GPT-5.6 Luna.*中/ });
    await user.click(updated);
    picker = screen.getByRole('dialog', { name: '选择模型与推理强度' });
    expect(within(picker).getByRole('radio', { name: '中' })).toBeChecked();
    await user.click(within(picker).getByRole('button', { name: /更换模型/ }));
    expect(within(picker).getByRole('searchbox', { name: '搜索模型' })).toBeInTheDocument();
    expect(within(picker).getByRole('option', { name: '选择模型 GPT-5.6 Luna' })).toHaveAttribute('aria-selected', 'true');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(updated).toHaveFocus());
  });

  it('does not animate an active Room record as a running turn on the home list', () => {
    const { container } = renderHome();
    expect(container.querySelector('.an-recent-card.is-room .is-run')).toBeNull();
  });

  it('restores all four Session permission modes and submits the managed project scope', async () => {
    const user = userEvent.setup();
    const { transport } = renderHome();

    await user.click(await screen.findByRole('button', { name: /权限 · 完全访问/ }));
    const menu = screen.getByRole('menu');
    expect(within(menu).getAllByRole('menuitemradio')).toHaveLength(4);
    expect(within(menu).getByRole('menuitemradio', { name: /^只读/ })).toBeInTheDocument();
    expect(within(menu).getByRole('menuitemradio', { name: /^完全访问/ })).toBeInTheDocument();
    expect(within(menu).getByRole('menuitemradio', { name: /^工作区托管/ })).toBeInTheDocument();
    expect(within(menu).getByRole('menuitemradio', { name: /^全自动/ })).toBeInTheDocument();

    await user.click(within(menu).getByRole('menuitemradio', { name: /^工作区托管/ }));
    await user.type(screen.getByRole('textbox', { name: '描述你想完成的工作' }), '在选定项目中完成任务');
    await user.click(screen.getByRole('button', { name: '开始 Session' }));

    await waitFor(() => expect(
      transport.requests.some(({ request }) => request.pathId === 'agent.sessions.create'),
    ).toBe(true));
    const create = transport.requests.find(
      ({ request }) => request.pathId === 'agent.sessions.create',
    )?.request;
    expect(create?.body).toMatchObject({
      mode: 'coordinator',
      executionMode: 'workspace_managed',
      toolProfileVersion: 'control-center-v1',
      workspaceRoots: ['/work/paw'],
      workspaceScopeConfirmation: 'APPROVE_WORKSPACE_SCOPE',
    });
  });

  it('keeps a pasted image visible, removable, and sends its managed receipt with a new Session', async () => {
    const user = userEvent.setup();
    const file = new File(['preview'], 'screen.png', { type: 'image/png' });
    const { transport } = renderHome({
      imagePaste: (input) => [{
        id: 'media-screen',
        name: 'screen.png',
        mimeType: 'image/png',
        byteSize: file.size,
        sha256: 'sha-screen',
        ...(input.sessionId ? { sessionId: input.sessionId } : {}),
      }],
    });
    const composer = screen.getByRole('textbox', { name: '描述你想完成的工作' });

    fireEvent.paste(composer, {
      clipboardData: { files: [file], items: [], getData: () => '' },
    });
    expect(screen.getByRole('list', { name: '待发送附件' })).toHaveTextContent('screen.png');
    await user.click(screen.getByRole('button', { name: '移除 screen.png' }));
    expect(screen.queryByRole('list', { name: '待发送附件' })).not.toBeInTheDocument();

    fireEvent.paste(composer, {
      clipboardData: { files: [file], items: [], getData: () => '' },
    });
    await user.type(composer, '请检查这张截图');
    await user.click(screen.getByRole('button', { name: '开始 Session' }));

    await waitFor(() => expect(transport.imagePasteCalls).toHaveLength(1));
    expect(transport.imagePasteCalls[0]).toMatchObject({
      sessionId: 'session-created',
      maxFiles: 1,
      files: [file],
    });
    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.prompt')).toBe(true));
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.model.select')).toBe(false);
    const prompt = transport.requests.find(({ request }) => request.pathId === 'agent.session.prompt')?.request;
    expect(prompt?.body).toMatchObject({ message: '请检查这张截图', attachments: ['media-screen'] });
  });

  it('hands off one optimistic first message before admission using the same client identity', async () => {
    let resolveAdmission!: (value: unknown) => void;
    const admission = new Promise<unknown>((resolve) => { resolveAdmission = resolve; });
    let projectionAtHandoff: ReturnType<typeof useAgentLiveStore.getState>['projections'][string] | undefined;
    const onCreated = vi.fn((selection: { kind: string; id?: string }) => {
      if (selection.kind === 'session' && selection.id) {
        projectionAtHandoff = homeProjection(transport, selection.id);
      }
    });
    const { transport } = renderHome({
      onCreated,
      promptRoute: () => admission,
    });
    const user = userEvent.setup();

    await user.type(screen.getByRole('textbox', { name: '描述你想完成的工作' }), '首条消息不能重复');
    await user.click(screen.getByRole('button', { name: '开始 Session' }));

    expect(onCreated).toHaveBeenCalledTimes(1);
    expect(projectionAtHandoff).toBeDefined();
    const optimisticEntries = Object.entries(projectionAtHandoff?.optimisticByClientMessageId ?? {});
    expect(optimisticEntries).toHaveLength(1);
    const [clientMessageId, messageId] = optimisticEntries[0]!;
    expect(projectionAtHandoff?.messagesById[messageId]?.blocks[0]?.data.text).toBe('首条消息不能重复');
    await waitFor(() => expect(
      transport.requests.some(({ request }) => request.pathId === 'agent.session.prompt'),
    ).toBe(true));
    const request = transport.requests.find(
      ({ request: item }) => item.pathId === 'agent.session.prompt',
    )?.request;
    expect(request?.body).toMatchObject({
      message: '首条消息不能重复',
      clientMessageId,
    });

    resolveAdmission({ ok: true });
    await waitFor(() => expect(
      homeProjection(transport)?.messagesById[messageId]?.status,
    ).toBe('queued'));
  });

  it('discards the first optimistic message when admission was synchronously cancelled', async () => {
    const { transport } = renderHome({
      promptRoute: {
        ok: true,
        accepted: false,
        cancelled: true,
        admissionCancelled: true,
      },
    });
    const user = userEvent.setup();
    await user.type(
      screen.getByRole('textbox', { name: '描述你想完成的工作' }),
      '这条首轮已被 Stop 取消',
    );
    await user.click(screen.getByRole('button', { name: '开始 Session' }));

    await waitFor(() => expect(
      transport.requests.filter(({ request }) => request.pathId === 'agent.session.prompt'),
    ).toHaveLength(1));
    await waitFor(() => {
      const projection = homeProjection(transport);
      expect(projection?.optimisticByClientMessageId).toEqual({});
      expect(projection?.messageOrder).toEqual([]);
      expect(projection?.turnOrder).toEqual([]);
      expect(projection?.status).toBe('idle');
    });
  });

  it('hands off the first attachment message before import resolves and later sends exact receipts', async () => {
    const file = new File(['preview'], 'slow.png', { type: 'image/png' });
    let resolveImport!: (value: PickedFile[]) => void;
    const imported = new Promise<PickedFile[]>((resolve) => { resolveImport = resolve; });
    let projectionAtHandoff: ReturnType<typeof useAgentLiveStore.getState>['projections'][string] | undefined;
    const onCreated = vi.fn((selection: { kind: string; id?: string }) => {
      if (selection.kind === 'session' && selection.id) {
        projectionAtHandoff = homeProjection(transport, selection.id);
      }
    });
    const { transport } = renderHome({
      onCreated,
      imagePaste: () => imported,
    });
    const user = userEvent.setup();
    const composer = screen.getByRole('textbox', { name: '描述你想完成的工作' });
    fireEvent.paste(composer, {
      clipboardData: { files: [file], items: [], getData: () => '' },
    });
    await user.type(composer, '先显示这条附件消息');
    await user.click(screen.getByRole('button', { name: '开始 Session' }));

    expect(onCreated).toHaveBeenCalledTimes(1);
    const optimisticEntries = Object.entries(projectionAtHandoff?.optimisticByClientMessageId ?? {});
    expect(optimisticEntries).toHaveLength(1);
    const [clientMessageId, messageId] = optimisticEntries[0]!;
    expect(projectionAtHandoff?.messagesById[messageId]).toMatchObject({
      attachments: [expect.stringMatching(/^home-attachment-/)],
    });
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.prompt')).toBe(false);

    resolveImport([{
      id: 'media-slow',
      name: 'slow.png',
      mimeType: 'image/png',
      byteSize: file.size,
      sha256: 'sha-slow',
      sessionId: 'session-created',
    }]);
    await waitFor(() => expect(
      transport.requests.some(({ request }) => request.pathId === 'agent.session.prompt'),
    ).toBe(true));
    const request = transport.requests.find(
      ({ request: item }) => item.pathId === 'agent.session.prompt',
    )?.request;
    expect(request?.body).toMatchObject({
      message: '先显示这条附件消息',
      attachments: ['media-slow'],
      clientMessageId,
    });
    expect(
      homeProjection(transport)?.messagesById[messageId]?.attachments,
    ).toEqual(['media-slow']);
  });

  it('keeps a late attachment import and admission failure in their original transport projection', async () => {
    let resolveImport!: (value: PickedFile[]) => void;
    let rejectAdmission!: (reason: unknown) => void;
    const imported = new Promise<PickedFile[]>((resolve) => { resolveImport = resolve; });
    const admission = new Promise<unknown>((_resolve, reject) => { rejectAdmission = reject; });
    const { transport } = renderHome({ imagePaste: () => imported, promptRoute: () => admission });
    const user = userEvent.setup();
    const file = new File(['image'], 'late.png', { type: 'image/png' });
    const composer = screen.getByRole('textbox', { name: '描述你想完成的工作' });
    fireEvent.paste(composer, { clipboardData: { files: [file], items: [], getData: () => '' } });
    await user.type(composer, '原连接的附件');
    await user.click(screen.getByRole('button', { name: '开始 Session' }));
    const [clientMessageId, messageId] = Object.entries(homeProjection(transport)!.optimisticByClientMessageId)[0];
    const transportB = new MockControlTransport();
    const addressB = agentSessionAddress(transportB, 'session-created');
    useAgentLiveStore.getState().appendOptimistic(addressB, {
      clientMessageId, text: '另一个连接的消息', attachments: ['media-b'], nowMs: 1,
    });
    const beforeB = homeProjection(transportB);

    resolveImport([{ id: 'media-a', name: 'late.png', mimeType: 'image/png', byteSize: file.size, sessionId: 'session-created' }]);
    await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.prompt')).toHaveLength(1));
    expect(homeProjection(transport)?.messagesById[messageId].attachments).toEqual(['media-a']);
    expect(homeProjection(transportB)).toBe(beforeB);
    rejectAdmission(new TypeError('fetch failed'));
    await waitFor(() => expect(homeProjection(transport)?.messagesById[messageId])
      .toMatchObject({ status: 'failed', admissionState: 'ambiguous' }));
    expect(homeProjection(transportB)).toBe(beforeB);
    expect(useAgentLiveStore.getState().projections['session-created']).toBeUndefined();
    expect(transportB.requests).toHaveLength(0);
  });

  it('reports an attachment import failure before admission without calling it a network-ambiguous prompt', async () => {
    const file = new File(['preview'], 'broken.png', { type: 'image/png' });
    const onCreated = vi.fn();
    const { transport } = renderHome({
      onCreated,
      imagePaste: () => Promise.reject(new TypeError('clipboard bytes unavailable')),
    });
    const user = userEvent.setup();
    const composer = screen.getByRole('textbox', { name: '描述你想完成的工作' });
    fireEvent.paste(composer, {
      clipboardData: { files: [file], items: [], getData: () => '' },
    });
    await user.type(composer, '附件导入失败要如实显示');
    await user.click(screen.getByRole('button', { name: '开始 Session' }));

    expect(onCreated).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      const message = Object.values(
        homeProjection(transport)?.messagesById ?? {},
      )[0];
      expect(message).toMatchObject({ status: 'failed' });
      expect(message).not.toHaveProperty('admissionState');
      expect(
        homeProjection(transport)
          ?.turnsById[message.turnId]?.failure,
      ).toContain('重新上传');
    });
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.prompt')).toHaveLength(0);
  });

  it.each([
    ['pending', 'in_flight'],
    ['unresolved', 'unresolved'],
  ] as const)('keeps a first prompt with %s admission state for durable reconciliation', async (expectedState, recoveryState) => {
    const { transport } = renderHome({
      promptRoute: (request: ControlRequest) => {
        const clientMessageId = String((request.body as Record<string, unknown>).clientMessageId);
        throw new ControlTransportHttpError(
          'agent.session.prompt',
          409,
          'receipt remains pending',
          {
            ok: false,
            code: 'AGENT_COMMAND_PENDING',
            commandReceipt: {
              state: 'pending',
              clientMessageId,
              recoveryState,
            },
          },
        );
      },
    });
    const user = userEvent.setup();
    await user.type(screen.getByRole('textbox', { name: '描述你想完成的工作' }), '不要误判这条消息失败');
    await user.click(screen.getByRole('button', { name: '开始 Session' }));

    await waitFor(() => {
      const projection = homeProjection(transport);
      const message = Object.values(projection?.messagesById ?? {}).find(
        (item) => item.clientMessageId === String(
          (transport.requests.find(({ request }) => request.pathId === 'agent.session.prompt')?.request.body as Record<string, unknown>)?.clientMessageId,
        ),
      );
      expect(message).toMatchObject({ status: 'queued', admissionState: expectedState });
    });
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.prompt')).toHaveLength(1);
  });

  it('marks an ambiguous first-prompt transport loss for verification without resending', async () => {
    const { transport } = renderHome({
      promptRoute: () => { throw new TypeError('fetch failed'); },
    });
    const user = userEvent.setup();
    await user.type(screen.getByRole('textbox', { name: '描述你想完成的工作' }), '这条消息可能已经送达');
    await user.click(screen.getByRole('button', { name: '开始 Session' }));

    await waitFor(() => expect(
      Object.values(homeProjection(transport)?.messagesById ?? {})[0],
    ).toMatchObject({ status: 'failed', admissionState: 'ambiguous' }));
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.prompt')).toHaveLength(1);
  });

  it('keeps a definitive first-prompt rejection as one retryable failed turn', async () => {
    const { transport } = renderHome({
      promptRoute: () => { throw new Error('provider rejected this prompt'); },
    });
    const user = userEvent.setup();
    await user.type(screen.getByRole('textbox', { name: '描述你想完成的工作' }), '保留确定失败的原始输入');
    await user.click(screen.getByRole('button', { name: '开始 Session' }));

    await waitFor(() => {
      const projection = homeProjection(transport);
      const messages = Object.values(projection?.messagesById ?? {});
      expect(messages).toHaveLength(1);
      expect(messages[0]).toMatchObject({ status: 'failed' });
      expect(messages[0]).not.toHaveProperty('admissionState');
      expect(messages[0]?.blocks[0]?.data.text).toBe('保留确定失败的原始输入');
      expect(Object.values(projection?.turnsById ?? {})[0]?.failure).toBe('provider rejected this prompt');
    });
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.prompt')).toHaveLength(1);
  });

  it('recomputes the visible Room team from a task suggestion and sends the adjusted participants', async () => {
    const user = userEvent.setup();
    const { transport } = renderHome({
      personas: Array.from({ length: 8 }, (_, index) => persona(`partner-${index + 1}`, `伙伴 ${index + 1}`)),
    });

    await user.click(screen.getByRole('radio', { name: 'Room' }));
    await user.type(
      screen.getByRole('textbox', { name: '描述你想完成的工作' }),
      '并行检查前端、后端、测试和发布流程',
    );
    expect(screen.getByText('任务建议 4 位')).toBeInTheDocument();
    const planned = screen.getAllByTestId('room-planned-participant');
    expect(planned).toHaveLength(4);
    expect(planned.map((item) => item.textContent)).toEqual(expect.arrayContaining([
      expect.stringContaining('Earth'),
      expect.stringContaining('Mars'),
      expect.stringContaining('Venus'),
      expect.stringContaining('Jupiter'),
    ]));
    expect(planned.some((item) => /伙伴 [1-4]/.test(item.textContent ?? ''))).toBe(false);

    const decrease = screen.getByRole('button', { name: '减少 Room 伙伴' });
    await user.click(decrease);
    await user.click(decrease);
    await user.click(decrease);
    expect(screen.getAllByTestId('room-planned-participant')).toHaveLength(1);
    expect(screen.getByRole('button', { name: '开始 Room' })).toBeDisabled();

    const increase = screen.getByRole('button', { name: '增加 Room 伙伴' });
    await user.click(increase);
    await user.click(increase);
    await user.click(increase);
    await user.click(increase);
    expect(screen.getAllByTestId('room-planned-participant')).toHaveLength(5);
    await user.click(screen.getByRole('button', { name: '开始 Room' }));

    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'agent.rooms.create')).toBe(true));
    const create = transport.requests.find(({ request }) => request.pathId === 'agent.rooms.create')?.request;
    expect((create?.body as { participants?: unknown[] }).participants).toHaveLength(5);
    expect(create?.body).toMatchObject({
      routingConfig: { maxResponders: 5 },
      permissionPolicy: {
        schemaVersion: 'rag-ime.room-permission-policy.v1',
        room: { executionMode: 'full_trust' },
        partner: { executionMode: 'inherit' },
        toolAgent: { executionMode: 'inherit' },
      },
      dangerousModeConfirmation: 'ENABLE_FULL_TRUST',
    });
    expect(create?.body).not.toHaveProperty('executionMode');
  });

  it('explains the default full permissions and starts Room without a project or another approval step', async () => {
    const user = userEvent.setup();
    const { transport } = renderHome({
      personas: [persona('partner-1', '伙伴 1'), persona('partner-2', '伙伴 2')],
      projectRoots: [],
    });

    await user.click(screen.getByRole('radio', { name: 'Room' }));
    expect(screen.getByLabelText('Room 执行权限')).toHaveTextContent(
      'Room、伙伴和 Tool Agent 均为全权限，开始后无需逐项批准。',
    );
    await user.type(screen.getByRole('textbox', { name: '描述你想完成的工作' }), '直接协作完成任务');
    await user.click(screen.getByRole('button', { name: '开始 Room' }));

    await waitFor(() => expect(transport.requests.filter(
      ({ request }) => request.pathId === 'agent.rooms.create',
    )).toHaveLength(1));
    expect(transport.requests.find(({ request }) => request.pathId === 'agent.rooms.create')?.request.body)
      .toMatchObject({
        workspaceRoots: ['/'],
        permissionPolicy: {
          room: { executionMode: 'full_trust' },
          partner: { executionMode: 'inherit' },
          toolAgent: { executionMode: 'inherit' },
        },
        dangerousModeConfirmation: 'ENABLE_FULL_TRUST',
      });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('shows and submits all Room permission layers while allowing explicit child narrowing', async () => {
    const user = userEvent.setup();
    const { transport } = renderHome({
      personas: [
        persona('partner-1', '伙伴 1'),
        persona('partner-2', '伙伴 2'),
      ],
    });

    await user.click(screen.getByRole('radio', { name: 'Room' }));
    const permission = screen.getByRole('button', { name: '权限 · 全自动 · 分层' });
    expect(permission).toBeInTheDocument();
    await user.click(permission);
    expect(screen.getByText('Room 边界')).toBeInTheDocument();
    expect(screen.getByText('行星 / Partner')).toBeInTheDocument();
    expect(screen.getByText('卫星 / Tool Agent')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Room 边界配置模式' })).toHaveValue('full_trust');
    expect(screen.getByRole('combobox', { name: '行星 / Partner配置模式' })).toHaveValue('inherit');
    expect(screen.getByRole('combobox', { name: '卫星 / Tool Agent配置模式' })).toHaveValue('inherit');
    await user.selectOptions(
      screen.getByRole('combobox', { name: '行星 / Partner配置模式' }),
      'per_action',
    );
    await user.selectOptions(
      screen.getByRole('combobox', { name: '卫星 / Tool Agent配置模式' }),
      'read_only',
    );
    expect(screen.getByRole('button', { name: '权限 · 自定义 · 分层' })).toBeInTheDocument();
    expect(screen.getByLabelText('Room 执行权限')).toHaveTextContent(
      'Room 全自动 · 伙伴 全权限 · Tool Agent 只读',
    );
    expect(screen.getByLabelText('Room 执行权限')).not.toHaveTextContent('均为全权限');
    await user.type(
      screen.getByRole('textbox', { name: '描述你想完成的工作' }),
      '按分层边界完成任务',
    );
    await user.click(screen.getByRole('button', { name: '开始 Room' }));

    await waitFor(() => expect(
      transport.requests.some(({ request }) => request.pathId === 'agent.rooms.create'),
    ).toBe(true));
    const create = transport.requests.find(
      ({ request }) => request.pathId === 'agent.rooms.create',
    )?.request;
    expect(create?.body).toMatchObject({
      permissionPolicy: {
        schemaVersion: 'rag-ime.room-permission-policy.v1',
        room: { executionMode: 'full_trust' },
        partner: { executionMode: 'per_action' },
        toolAgent: { executionMode: 'read_only' },
      },
    });
    expect(create?.body).not.toHaveProperty('executionMode');
  });
});

function homeProjection(transport: ControlTransport, sessionId = 'session-created') {
  return selectAgentProjection(useAgentLiveStore.getState(), agentSessionAddress(transport, sessionId));
}

function renderHome({
  interfaceMode = 'traditional',
  durableAvailable = false,
  modelReference = 'inherit',
  models = [],
  personas = [],
  projectRoots = ['/work/paw'],
  imagePaste,
  onCreated = vi.fn(),
  promptRoute = { ok: true },
  continuityRoute,
  createRoute,
}: {
  interfaceMode?: 'traditional' | 'jev';
  durableAvailable?: boolean;
  modelReference?: string;
  models?: PiModelOption[];
  personas?: AgentPersonaV1[];
  projectRoots?: string[];
  imagePaste?: (input: AgentImagePasteOptions) => PickedFile[] | Promise<PickedFile[]>;
  onCreated?: Parameters<typeof PawAgentHome>[0]['onCreated'];
  promptRoute?: MockRouteHandler;
  continuityRoute?: MockRouteHandler;
  createRoute?: MockRouteHandler;
} = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const transport = new MockControlTransport({
    routes: {
      'configuration.settings': {
        ok: true,
        settings: { agent: { defaults: { modelReference, thinkingLevel: 'high', executionMode: 'per_action' } } },
        runtimeConfig: { runtimeRevision: 7 },
      },
      'agent.rooms.create': (request: ControlRequest) => ({
        ok: true,
        room: {
          id: 'room-created',
          title: 'Room',
          participants: (request.body as { participants?: unknown[] }).participants ?? [],
        },
      }),
      'agent.room.message': { ok: true },
      'agent.jev.command': { ok: true, accepted: true, graphId: 'jev-created', rootId: 'jev-root' },
      'agent.sessions.create': createRoute ?? {
        ok: true,
        session: {
          id: 'session-created',
          title: '新工作',
          mode: 'assistant',
          status: 'running',
          workspaceRoots: [],
        },
      },
      'agent.session.prompt': promptRoute,
      ...(continuityRoute ? { 'agent.continuity.read': continuityRoute } : {}),
    },
    ...(imagePaste ? { imagePaste } : {}),
  });
  const rendered = render(
    <QueryClientProvider client={client}>
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>
          <PawAgentHome
            interfaceMode={interfaceMode}
            durableAvailable={durableAvailable}
            defaultModel="gpt/gpt-5.6-luna"
            models={models}
            personas={personas}
            projectRoots={projectRoots}
            rooms={[room()]}
            sessions={[session()]}
            onCreated={onCreated}
            onOpenRoom={vi.fn()}
            onOpenSession={vi.fn()}
          />
        </TooltipProvider>
      </ControlTransportProvider>
    </QueryClientProvider>,
  );
  return { ...rendered, transport };
}

function model(id: string, name: string): PiModelOption {
  return { id, name, provider: 'gpt', reference: `gpt/${id}`, thinkingLevels: ['off', 'medium', 'high'] };
}

function persona(roleId: string, displayName: string): AgentPersonaV1 {
  return {
    schemaVersion: 'rag-ime.agent-persona.v1',
    roleId,
    version: '1',
    displayName,
    tagline: `${displayName}伙伴`,
    summary: '',
    traits: ['协作'],
    selectableModes: ['assistant', 'coordinator'],
    defaults: {
      modelPolicy: 'inherit', memoryPolicy: 'inherit', thinkingLevel: 'high',
      toolProfileVersion: 'control-center-v1',
    },
    runtimeCharacteristics: {
      intelligence: 'balanced', speed: 'balanced', context: 'workspace',
      suitableTasks: ['协作'], unsuitableTasks: ['无'], isDefault: false,
    },
    visualProfile: { accentToken: 'blue', avatarAssetId: '', symbolName: 'bot' },
    safetyPolicyVersion: 'agent-core-v2',
  };
}

function session(): SessionSummary {
  return {
    id: 'session-1',
    title: '发布检查',
    mode: 'coordinator',
    status: 'idle',
    roleId: '',
    roleVersion: '',
    roleBookRevisionId: '',
    updatedAtMs: 2,
    workspaceRoots: ['/work/paw'],
    lastMessagePreview: '检查构建结果',
    executionMode: 'per_action',
  } as SessionSummary;
}

function room(): RoomSummary {
  return {
    id: 'room-1',
    title: '迁移作战室',
    status: 'active',
    description: '并行迁移与审查',
    routingPolicy: 'parallel',
    moderatorParticipantId: 'p1',
    updatedAtMs: 3,
    workspaceRoots: ['/work/paw'],
    participants: [
      { id: 'p1', sessionId: 's1', roleId: 'builder', roleVersion: '1', displayName: '构建者', status: 'active', ordinal: 0 },
      { id: 'p2', sessionId: 's2', roleId: 'reviewer', roleVersion: '1', displayName: '审阅者', status: 'active', ordinal: 1 },
    ],
  } as RoomSummary;
}
