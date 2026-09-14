import { forwardRef, type ForwardedRef, type Key, type ReactNode } from 'react';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { useAgentLiveStore } from '@/features/agent/state/live-store';
import { TeamProvider } from '@/features/team/team-context';
import { TeamApi } from '@/features/team/team-api';
import { MockControlTransport, type MockRouteHandler } from '@/test/mock-transport';
import type { ControlRequest } from '@/platform/transport';
import type { TeamSession, TeamSessionResourceSnapshot } from '@/features/team/types';
import manifest from './pawos-app.json';
import ZhangguiWenshuApp from './App';

const { sessionWorkspaceProps, renderRealWorkspace } = vi.hoisted(() => ({
  sessionWorkspaceProps: vi.fn(),
  renderRealWorkspace: { current: false },
}));

vi.mock('@/paw-os/apps/PawSessionWorkspace', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/paw-os/apps/PawSessionWorkspace')>();
  return {
    ...actual,
    PawSessionWorkspace: (props: Parameters<typeof actual.PawSessionWorkspace>[0]) => {
      sessionWorkspaceProps(props);
      return renderRealWorkspace.current
        ? <actual.PawSessionWorkspace {...props} />
        : <section data-testid="shared-session">Session {props.recordId}</section>;
    },
  };
});

// jsdom has no row height; keep the real Session, timeline, and recovery actions
// while rendering the virtualizer's rows so these checks see the user's text.
vi.mock('react-virtuoso', () => ({
  Virtuoso: forwardRef(function MockVirtuoso({ components, computeItemKey, context, data, itemContent, scrollerRef }: {
    components?: { Header?: (props: { context?: unknown }) => ReactNode; Footer?: () => ReactNode };
    computeItemKey?: (index: number, item: string) => Key;
    context?: unknown;
    data: string[];
    itemContent: (index: number, item: string) => ReactNode;
    scrollerRef?: (scroller: HTMLElement | Window | null) => void;
  }, _ref: ForwardedRef<unknown>) {
    const Header = components?.Header;
    const Footer = components?.Footer;
    return <div ref={(node) => scrollerRef?.(node)}>
      {Header ? <Header context={context} /> : null}
      {data.map((item, index) => <div key={computeItemKey?.(index, item) ?? index}>{itemContent(index, item)}</div>)}
      {Footer ? <Footer /> : null}
    </div>;
  }),
}));

afterEach(() => {
  cleanup();
  document.querySelector('meta[name="paw-deployment"]')?.remove();
  window.localStorage.clear();
  sessionWorkspaceProps.mockClear();
  renderRealWorkspace.current = false;
  useAgentLiveStore.setState({ projections: {} });
});

describe('掌柜问数 Extension App', () => {
  it('preserves each unsent mode draft while tabs support keyboard navigation', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: { 'agent.sessions.list': { ok: true, items: [] } } });
    renderApp(transport);
    await user.type(await screen.findByRole('textbox', { name: '问数问题' }), '本月收入是多少');
    screen.getByRole('tab', { name: '问数' }).focus();
    await user.keyboard('{ArrowRight}');
    expect(screen.getByRole('tab', { name: '对账' })).toHaveFocus();
    expect(screen.getByRole('tab', { name: '问数' })).toHaveAttribute('tabindex', '-1');
    await user.type(screen.getByRole('textbox', { name: '对账问题' }), '核对两份报表');
    await user.click(screen.getByRole('tab', { name: '问数' }));
    expect(screen.getByRole('textbox', { name: '问数问题' })).toHaveValue('本月收入是多少');
    await user.click(screen.getByRole('tab', { name: '对账' }));
    expect(screen.getByRole('textbox', { name: '对账问题' })).toHaveValue('核对两份报表');
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.sessions.create')).toBe(false);
  });

  it('retries a failed owner-scoped history read without creating another conversation', async () => {
    const user = userEvent.setup();
    let offline = true;
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': () => {
        if (offline) throw new Error('对话记录暂时不可用');
        return { ok: true, items: [{
          id: 'session-restored', title: '问数记录', mode: 'assistant', status: 'idle', updatedAtMs: 1,
          surfaceKind: 'extension_app', ownerAppId: manifest.id, surfaceKey: 'ask',
        }] };
      },
    } });
    renderApp(transport);
    const alert = await screen.findByRole('alert');
    offline = false;
    await user.click(within(alert).getByRole('button', { name: '重新读取' }));
    expect(await screen.findByTestId('shared-session')).toHaveTextContent('session-restored');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.sessions.create')).toBe(false);
  });

  it('keeps three conversation modes in one source-isolated App surface', async () => {
    renderApp(new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [] },
    } }));

    expect(await screen.findByRole('heading', { name: '掌柜问数' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '问数' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: '对账' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '解释' })).toBeInTheDocument();
    expect(await screen.findByRole('textbox', { name: '问数问题' })).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: '启动前运行受管沙箱自测' })).not.toBeChecked();
    expect(screen.getByText(/SGG 仅用于沙盒自测/)).toBeInTheDocument();
  });

  it('binds the installed App managed source without opening a directory picker', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [] },
    } });

    renderApp(transport);

    expect(await screen.findByText('掌柜问数受控数据')).toBeInTheDocument();
    expect(screen.getByText('已选择受控数据')).toBeInTheDocument();
    expect(screen.getByText(/默认绑定掌柜问数受控数据.*SGG.*fixture-v2.*只读沙箱/)).toBeInTheDocument();
    expect(transport.filePickCalls).toEqual([]);
  });

  it('keeps the managed source usable when the current host cannot migrate to a directory', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [] },
    } });
    Object.defineProperty(transport, 'pickFiles', { configurable: true, value: undefined });

    renderApp(transport);

    const source = await screen.findByRole('button', { name: /当前数据源.*当前宿主不支持更换/ });
    expect(source).toBeDisabled();
    expect(screen.getByRole('textbox', { name: '问数问题' })).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('opens the directory picker only after an explicit source migration', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      pickedFiles: [{
        id: 'workspace:sales-data',
        name: 'sales-data',
        mimeType: 'inode/directory',
        byteSize: 0,
        path: '/work/sales-data',
      }],
      routes: {
        'agent.sessions.list': { ok: true, items: [] },
      },
    });

    renderApp(transport);

    expect(await screen.findByText('掌柜问数受控数据')).toBeInTheDocument();
    expect(transport.filePickCalls).toEqual([]);
    await user.click(screen.getByRole('button', { name: '迁移到数据目录' }));

    expect(transport.filePickCalls).toEqual([expect.objectContaining({
      purpose: 'workspace-root',
      selection: 'directory',
    })]);
    expect(await screen.findByText('sales-data')).toBeInTheDocument();
    expect(screen.getByText('已选择数据目录')).toBeInTheDocument();
  });

  it('creates one ordinary Pi Session, enables the App Skill, and sends the selected mode contract', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [] },
      'agent.sessions.create': {
        ok: true,
        session: { id: 'session-zhanggui', title: '掌柜问数 · 对账', mode: 'assistant', status: 'idle', updatedAtMs: 1 },
      },
      'agent.session.mode.update': { ok: true },
      'extension.sandbox.experiment.run': {
        schemaVersion: 'rag-ime.extension-sandbox-experiment-receipt.v1',
        ok: true,
        sessionId: 'session-zhanggui',
        ownerAppId: manifest.id,
        candidateBindingSha256: manifest.bindingSha256,
        requestedDecision: 'skip',
        executed: false,
        executionStatus: 'skipped',
      },
      'agent.session.prompt': { ok: true },
    } });
    renderApp(transport);

    await user.click(await screen.findByRole('tab', { name: '对账' }));
    const input = screen.getByRole('textbox', { name: '对账问题' });
    await user.type(input, '核对销售台账与回款表');
    await user.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByTestId('shared-session')).toHaveTextContent('session-zhanggui');
    expect(sessionWorkspaceProps).toHaveBeenLastCalledWith(expect.objectContaining({
      appearance: 'embedded',
      composerPlaceholder: expect.stringContaining('核对'),
    }));
    const calls = transport.requests.map(({ request }) => request.pathId);
    expect(calls).toEqual([
      'agent.sessions.list',
      'agent.sessions.create',
      'agent.session.mode.update',
      'extension.sandbox.experiment.run',
      'agent.session.prompt',
    ]);
    const mode = requestFor(transport, 'agent.session.mode.update');
    expect(mode.body).toMatchObject({
      mode: 'assistant',
      executionMode: 'per_action',
      piSkillsEnabled: true,
      projectContextEnabled: false,
      codexSkillsEnabled: false,
    });
    const prompt = requestFor(transport, 'agent.session.prompt');
    const list = requestFor(transport, 'agent.sessions.list');
    expect(list.query).toMatchObject({
      surfaceKind: 'extension_app',
      ownerAppId: manifest.id,
    });
    const create = requestFor(transport, 'agent.sessions.create');
    expect(create.body).toMatchObject({
      surfaceKind: 'extension_app',
      ownerAppId: manifest.id,
      surfaceKey: 'reconcile',
    });
    expect(requestFor(transport, 'extension.sandbox.experiment.run').body).toMatchObject({
      sessionId: 'session-zhanggui',
      ownerAppId: manifest.id,
      candidateBindingSha256: manifest.bindingSha256,
      requestedDecision: 'skip',
    });
    expect(prompt.params).toEqual({ sessionId: 'session-zhanggui' });
    expect(prompt.body).toMatchObject({ delivery: 'prompt' });
    expect(String((prompt.body as Record<string, unknown>).message)).toContain('`zhanggui-wenshu` Skill');
    expect(String((prompt.body as Record<string, unknown>).message)).toContain('当前掌柜问数模式：对账');
    expect(String((prompt.body as Record<string, unknown>).message)).toContain('默认绑定受控数据源：掌柜问数受控数据 · SGG fixture-v2');
    expect(String((prompt.body as Record<string, unknown>).message)).toContain('核对销售台账与回款表');
    expect(window.localStorage.length).toBe(0);
    expect(screen.queryByRole('button', { name: '打开运行详情' })).not.toBeInTheDocument();
  });

  it.each(['mode', 'sandbox'] as const)('reuses the created Session after %s preparation fails and explicitly resumes every unfinished step before prompting', async (failedStep) => {
    const user = userEvent.setup();
    let modeCalls = 0;
    let sandboxCalls = 0;
    const sessionId = `session-preparing-${failedStep}`;
    const transport = firstPromptTransport(sessionId, { ok: true }, {
      'agent.session.mode.update': () => {
        modeCalls += 1;
        if (failedStep === 'mode' && modeCalls === 1) throw new Error('模式准备失败');
        return { ok: true };
      },
      'extension.sandbox.experiment.run': () => {
        sandboxCalls += 1;
        if (failedStep === 'sandbox' && sandboxCalls === 1) throw new Error('沙箱准备失败');
        return {
          schemaVersion: 'rag-ime.extension-sandbox-experiment-receipt.v1', ok: true,
          sessionId, ownerAppId: manifest.id, candidateBindingSha256: manifest.bindingSha256,
          requestedDecision: 'run', executed: true, executionStatus: 'completed',
          sandboxRunId: 'sandbox-prepared', traceId: 'trace-prepared', evalRunId: 'eval-prepared',
        };
      },
    });
    renderApp(transport);
    await user.click(await screen.findByRole('checkbox', { name: '启动前运行受管沙箱自测' }));
    await user.type(screen.getByRole('textbox', { name: '问数问题' }), '保留准备失败后的原问题');
    await user.click(screen.getByRole('button', { name: '发送' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(failedStep === 'mode' ? '模式准备失败' : '沙箱准备失败');
    expect(screen.getByRole('textbox', { name: '问数问题' })).toHaveValue('保留准备失败后的原问题');
    expect(promptRequests(transport)).toHaveLength(0);
    expect(sandboxCalls).toBe(failedStep === 'mode' ? 0 : 1);
    await user.click(screen.getByRole('button', { name: '发送' }));
    await screen.findByTestId('shared-session');
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.create')).toHaveLength(1);
    expect(modeCalls).toBe(failedStep === 'mode' ? 2 : 1);
    expect(sandboxCalls).toBe(failedStep === 'sandbox' ? 2 : 1);
    expect(promptRequests(transport)).toHaveLength(1);
    expect(promptRequests(transport)[0].params).toEqual({ sessionId });
    const preparations = transport.requests.filter(({ request }) => request.pathId === 'extension.sandbox.experiment.run').map(({ request }) => request);
    for (const request of preparations) expect(request.body).toMatchObject({ sessionId, requestedDecision: 'run' });
    if (failedStep === 'sandbox') expect(preparations[1].body).toEqual(preparations[0].body);
  });

  it('keeps the first question in the only input until its admission receipt arrives', async () => {
    const user = userEvent.setup();
    const receipt = deferred<unknown>();
    const transport = firstPromptTransport('session-first-pending', () => receipt.promise);
    renderRealWorkspace.current = true;
    renderApp(transport);
    await user.type(await screen.findByRole('textbox', { name: '问数问题' }), '保留正在发送的原始问题');
    await user.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(promptRequests(transport)).toHaveLength(1));
    expect(screen.getByRole('textbox', { name: '问数问题' })).toHaveValue('保留正在发送的原始问题');
    expect(screen.getByRole('textbox', { name: '问数问题' })).toHaveAttribute('readonly');
    expect(screen.getAllByRole('textbox')).toHaveLength(1);
    expect(promptRequests(transport)).toHaveLength(1);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.create')).toHaveLength(1);

    await act(async () => receipt.resolve({ ok: true }));
    expect(await screen.findByText('保留正在发送的原始问题')).toBeVisible();
  });

  it.each(['conflict', 'cancelled'] as const)('retains a %s first prompt in the original input and sends only an explicit new request to the already prepared Session', async (outcome) => {
    const user = userEvent.setup();
    let attempts = 0;
    const transport = firstPromptTransport(`session-first-${outcome}`, (request: ControlRequest) => {
      attempts += 1;
      if (attempts > 1) return { ok: true };
      if (outcome === 'cancelled') return { accepted: false, cancelled: true, admissionCancelled: true };
      throw Object.assign(new Error('消息内容冲突'), {
        status: 409,
        payload: {
          code: 'AGENT_COMMAND_CONFLICT',
          commandReceipt: { state: 'conflict', recoveryState: 'new_command_required', clientMessageId: (request.body as Record<string, unknown>).clientMessageId },
        },
      });
    });
    renderRealWorkspace.current = true;
    renderApp(transport);
    await user.type(await screen.findByRole('textbox', { name: '问数问题' }), '明确未接收时保留的原问题');
    await user.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(outcome === 'cancelled' ? '已取消' : '输入已保留');
    const input = screen.getByRole('textbox', { name: '问数问题' });
    expect(input).toHaveValue('明确未接收时保留的原问题');
    expect(screen.queryByText('本轮未完成')).not.toBeInTheDocument();
    expect(promptRequests(transport)).toHaveLength(1);
    const original = promptRequests(transport)[0];
    await user.type(input, '，补充口径');
    await user.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(promptRequests(transport)).toHaveLength(2));
    const next = promptRequests(transport)[1];
    expect(next.params).toEqual(original.params);
    expect((next.body as Record<string, unknown>).message).toContain('明确未接收时保留的原问题，补充口径');
    expect((next.body as Record<string, unknown>).clientMessageId).not.toBe((original.body as Record<string, unknown>).clientMessageId);
    expect(next.body).not.toHaveProperty('retryOfClientMessageId');
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.create')).toHaveLength(1);
    expect(transport.requests.filter(({ request }) => request.pathId === 'extension.sandbox.experiment.run')).toHaveLength(1);
  });

  it('recovers a definitively rejected first prompt through the existing same-Session retry with its exact App context', async () => {
    const user = userEvent.setup();
    let attempts = 0;
    const transport = firstPromptTransport('session-first-rejected', (request: ControlRequest) => {
      attempts += 1;
      if (attempts === 1) throw Object.assign(new Error('本次发送被拒绝'), {
        status: 400,
        payload: {
          code: 'AGENT_COMMAND_FAILED',
          commandReceipt: { state: 'failed', clientMessageId: (request.body as Record<string, unknown>).clientMessageId },
        },
      });
      return { ok: true };
    });
    renderRealWorkspace.current = true;
    renderApp(transport);
    await user.type(await screen.findByRole('textbox', { name: '问数问题' }), '明确拒绝后仍能恢复的问题');
    await user.click(screen.getByRole('button', { name: '发送' }));

    const retry = await screen.findByRole('button', { name: '重试本轮' });
    expect(screen.getByText('明确拒绝后仍能恢复的问题')).toBeVisible();
    expect(promptRequests(transport)).toHaveLength(1);
    const original = promptRequests(transport)[0];
    await user.click(retry);
    await waitFor(() => expect(promptRequests(transport)).toHaveLength(2));
    const retried = promptRequests(transport)[1];
    expect(retried.params).toEqual(original.params);
    expect(retried.body).toMatchObject({
      message: (original.body as Record<string, unknown>).message,
      retryOfClientMessageId: (original.body as Record<string, unknown>).clientMessageId,
    });
    expect((retried.body as Record<string, unknown>).clientMessageId).not.toBe((original.body as Record<string, unknown>).clientMessageId);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.create')).toHaveLength(1);
    expect(transport.requests.filter(({ request }) => request.pathId === 'extension.sandbox.experiment.run')).toHaveLength(1);
  });

  it.each(['in_flight', 'unresolved'] as const)('keeps a %s first-prompt receipt visible without offering an automatic or duplicate send', async (recoveryState) => {
    const user = userEvent.setup();
    const transport = firstPromptTransport(`session-first-${recoveryState}`, (request: ControlRequest) => {
      throw Object.assign(new Error('接收状态尚未确认'), {
        status: 409,
        payload: {
          code: 'AGENT_COMMAND_PENDING',
          commandReceipt: { state: 'pending', recoveryState, clientMessageId: (request.body as Record<string, unknown>).clientMessageId },
        },
      });
    });
    renderRealWorkspace.current = true;
    renderApp(transport);
    await user.type(await screen.findByRole('textbox', { name: '问数问题' }), '保留接收状态未知的问题');
    await user.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText('正在确认接收状态')).toBeVisible();
    expect(screen.getByText('保留接收状态未知的问题')).toBeVisible();
    expect(screen.queryByRole('button', { name: '重试本轮' })).not.toBeInTheDocument();
    expect(screen.queryByText('本轮未完成')).not.toBeInTheDocument();
    expect(promptRequests(transport)).toHaveLength(1);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.create')).toHaveLength(1);
  });

  it('verifies an ambiguous first prompt only on explicit retry and reconciles a matching durable receipt into one visible question', async () => {
    const user = userEvent.setup();
    const sessionId = 'session-first-ambiguous';
    const transport = firstPromptTransport(sessionId, () => { throw new TypeError('fetch failed'); });
    renderRealWorkspace.current = true;
    renderApp(transport);
    await user.type(await screen.findByRole('textbox', { name: '问数问题' }), '网络回执丢失时的原始问题');
    await user.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText(/暂时无法确认是否已接收/)).toBeVisible();
    expect(screen.getByText('网络回执丢失时的原始问题')).toBeVisible();
    expect(screen.queryByText('掌柜问数没有开始，请重试。')).not.toBeInTheDocument();
    expect(promptRequests(transport)).toHaveLength(1);
    const body = promptRequests(transport)[0].body as Record<string, unknown>;
    await user.click(screen.getByRole('button', { name: '重试本轮' }));
    await waitFor(() => expect(promptRequests(transport)).toHaveLength(2));
    expect(promptRequests(transport)[1].body).toMatchObject({ message: body.message, clientMessageId: body.clientMessageId });
    expect(promptRequests(transport)[1].body).not.toHaveProperty('retryOfClientMessageId');
    expect(await screen.findByText(/暂时无法确认是否已接收/)).toBeVisible();
    const local = Object.values(useAgentLiveStore.getState().projections[sessionId].messagesById)[0];
    act(() => useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [{ ...local, id: 'durable-first-question', status: 'completed', admissionState: undefined, clientMessageId: body.clientMessageId }],
      liveEvents: [], lastSequence: 1, resumeToken: `${sessionId}:1`, status: 'idle',
    }));
    expect(screen.getAllByText('网络回执丢失时的原始问题')).toHaveLength(1);
    expect(screen.queryByText(/暂时无法确认是否已接收/)).not.toBeInTheDocument();
    expect(promptRequests(transport)).toHaveLength(2);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.create')).toHaveLength(1);
  });

  it('runs the selected managed sandbox before handing the first prompt to Pi', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [] },
      'agent.sessions.create': {
        ok: true,
        session: { id: 'session-sandbox', title: '掌柜问数 · 问数', mode: 'assistant', status: 'idle', updatedAtMs: 1 },
      },
      'agent.session.mode.update': { ok: true },
      'extension.sandbox.experiment.run': {
        schemaVersion: 'rag-ime.extension-sandbox-experiment-receipt.v1',
        ok: true,
        sessionId: 'session-sandbox',
        ownerAppId: manifest.id,
        candidateBindingSha256: manifest.bindingSha256,
        requestedDecision: 'run',
        executed: true,
        executionStatus: 'completed',
        sandboxRunId: 'sandbox:sgg:test',
        traceId: 'trace:sgg:test',
        evalRunId: 'eval:sgg:test',
      },
      'agent.session.prompt': { ok: true },
    } });
    renderApp(transport);

    await user.click(await screen.findByRole('checkbox', { name: '启动前运行受管沙箱自测' }));
    await user.type(screen.getByRole('textbox', { name: '问数问题' }), '本月销售额是多少？');
    await user.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(requestFor(transport, 'extension.sandbox.experiment.run').body).toMatchObject({
      sessionId: 'session-sandbox',
      ownerAppId: manifest.id,
      candidateBindingSha256: manifest.bindingSha256,
      requestedDecision: 'run',
    }));
    const calls = transport.requests.map(({ request }) => request.pathId);
    expect(calls.indexOf('extension.sandbox.experiment.run')).toBeLessThan(calls.indexOf('agent.session.prompt'));
    expect(await screen.findByRole('status')).toHaveTextContent('sandbox:sgg:test');
    await user.click(screen.getByRole('tab', { name: '对账' }));
    expect(screen.queryByText(/sandbox:sgg:test/)).not.toBeInTheDocument();
  });

  it('refuses a mismatched sandbox receipt and never sends the Pi prompt', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [] },
      'agent.sessions.create': {
        ok: true,
        session: { id: 'session-invalid-receipt', title: '掌柜问数 · 问数', mode: 'assistant', status: 'idle', updatedAtMs: 1 },
      },
      'agent.session.mode.update': { ok: true },
      'extension.sandbox.experiment.run': {
        schemaVersion: 'rag-ime.extension-sandbox-experiment-receipt.v1',
        ok: true,
        sessionId: 'session-invalid-receipt',
        ownerAppId: manifest.id,
        candidateBindingSha256: manifest.bindingSha256,
        requestedDecision: 'run',
        executed: false,
        executionStatus: 'skipped',
      },
      'agent.session.prompt': { ok: true },
    } });
    renderApp(transport);

    await user.click(await screen.findByRole('checkbox', { name: '启动前运行受管沙箱自测' }));
    await user.type(screen.getByRole('textbox', { name: '问数问题' }), '本月销售额是多少？');
    await user.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('沙箱');
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.prompt')).toBe(false);
  });

  it('restores only App-owned conversations from the durable Session owner query', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': {
        ok: true,
        items: [
          {
            id: 'session-app-ask',
            title: '掌柜问数 · 问数',
            mode: 'assistant',
            status: 'idle',
            updatedAtMs: 12,
            surfaceKind: 'extension_app',
            ownerAppId: manifest.id,
            surfaceKey: 'ask',
          },
          {
            id: 'session-agent',
            title: '普通 Agent 对话',
            mode: 'assistant',
            status: 'idle',
            updatedAtMs: 11,
            surfaceKind: 'agent',
            ownerAppId: '',
            surfaceKey: '',
          },
        ],
      },
    } });

    renderApp(transport);

    expect(await screen.findByTestId('shared-session')).toHaveTextContent('session-app-ask');
    expect(screen.queryByText('session-agent')).not.toBeInTheDocument();
    expect(requestFor(transport, 'agent.sessions.list').query).toMatchObject({
      surfaceKind: 'extension_app',
      ownerAppId: manifest.id,
    });
    expect(screen.getByText('已选择受控数据')).toBeInTheDocument();
    expect(screen.queryByText('对话已连接')).not.toBeInTheDocument();
  });

  it('uses the current Team project data and workspace-managed Session without sandbox or host-directory routes', async () => {
    const user = userEvent.setup();
    const project: TeamSession['spaces'][number] = {
      id: 'project-team', kind: 'project', name: '官网', role: 'contributor', revision: 1,
    };
    const teamSession: TeamSession = {
      user: { id: 'user-team', username: 'xiaowang', displayName: '小王', role: 'member', active: true },
      csrfToken: 'csrf-team',
      spaces: [project],
    };
    const ownSession = {
      id: 'session-team-own',
      title: '掌柜问数 · 问数',
      mode: 'coordinator',
      status: 'idle',
      updatedAtMs: 12,
      surfaceKind: 'extension_app',
      ownerAppId: manifest.id,
      surfaceKey: 'ask',
      ownerUserId: 'user-team',
      spaceId: project.id,
      canControl: true,
      audience: 'project',
    };
    const otherSession = { ...ownSession, id: 'session-team-other', ownerUserId: 'user-other', canControl: false };
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.create': { ok: true, session: { id: 'session-team-new', title: '掌柜问数 · 对账', status: 'idle', updatedAtMs: 1 } },
      'agent.session.mode.update': { ok: true },
      'agent.session.prompt': { ok: true },
    } });
    const api = fakeTeamApi(teamSession, [ownSession, otherSession]);
    vi.mocked(api.getSessionResourceSnapshot).mockResolvedValue(teamExtensionSnapshot(project.id, ownSession.id));
    setTeamDeploymentMeta();
    renderApp(transport, teamSession, api);

    expect(await screen.findByTestId('shared-session')).toHaveTextContent('session-team-own');
    expect(screen.queryByText('session-team-other')).not.toBeInTheDocument();
    expect(screen.queryByRole('checkbox', { name: '启动前运行受管沙箱自测' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /迁移到数据目录|更换数据目录/ })).not.toBeInTheDocument();
    expect(transport.filePickCalls).toEqual([]);
    expect(api.listProjectSessions).toHaveBeenCalledWith(project.id, 'csrf-team', {
      surfaceKind: 'extension_app',
      ownerAppId: manifest.id,
      includeArchived: false,
      limit: 100,
    });
    expect(api.getSessionResourceSnapshot).toHaveBeenCalledWith(project.id, ownSession.id, 'csrf-team');
    expect(transport.requests).toEqual([]);

    await user.click(screen.getByRole('button', { name: '新对话' }));
    await user.click(screen.getByRole('tab', { name: '对账' }));
    await user.type(screen.getByRole('textbox', { name: '对账问题' }), '项目中的问题');
    await user.click(screen.getByRole('button', { name: '发送' }));
    await screen.findByTestId('shared-session');

    expect(requestFor(transport, 'agent.sessions.create').body).toMatchObject({
      mode: 'coordinator',
      executionMode: 'workspace_managed',
      workspaceRoots: [],
      piSkillsEnabled: false,
      codexSkillsEnabled: false,
      surfaceKind: 'extension_app',
      ownerAppId: manifest.id,
    });
    expect(requestFor(transport, 'agent.session.mode.update').body).toMatchObject({
      mode: 'coordinator',
      executionMode: 'workspace_managed',
      workspaceRoots: [],
      piSkillsEnabled: false,
      codexSkillsEnabled: false,
    });
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.sessions.list')).toBe(false);
    expect(transport.requests.some(({ request }) => request.pathId === 'extension.sandbox.experiment.run')).toBe(false);
  });

  it('does not restore an App Session whose Team resource snapshot has no matching fixed version', async () => {
    const project: TeamSession['spaces'][number] = {
      id: 'project-team', kind: 'project', name: '官网', role: 'viewer', revision: 1,
    };
    const teamSession: TeamSession = {
      user: { id: 'user-team', username: 'xiaowang', displayName: '小王', role: 'member', active: true },
      csrfToken: 'csrf-team',
      spaces: [project],
    };
    const session = {
      id: 'session-team-old', title: '旧版本掌柜问数', mode: 'coordinator', status: 'idle', updatedAtMs: 2,
      surfaceKind: 'extension_app', ownerAppId: manifest.id, surfaceKey: 'ask',
      ownerUserId: 'user-team', spaceId: project.id, canControl: true, audience: 'project',
    };
    const transport = new MockControlTransport();
    const api = fakeTeamApi(teamSession, [session]);
    vi.mocked(api.getSessionResourceSnapshot).mockResolvedValue(teamExtensionSnapshot(project.id, session.id, { items: [] }));
    setTeamDeploymentMeta();
    renderApp(transport, teamSession, api);

    expect(await screen.findByRole('alert')).toHaveTextContent('固定资源');
    expect(screen.queryByTestId('shared-session')).not.toBeInTheDocument();
    expect(api.getSessionResourceSnapshot).toHaveBeenCalledWith(project.id, session.id, 'csrf-team');
    expect(transport.requests).toEqual([]);
  });

  it('stops a Team creation chain after unmount before the create receipt arrives', async () => {
    const project: TeamSession['spaces'][number] = {
      id: 'project-team', kind: 'project', name: '官网', role: 'contributor', revision: 1,
    };
    const teamSession: TeamSession = {
      user: { id: 'user-team', username: 'xiaowang', displayName: '小王', role: 'member', active: true },
      csrfToken: 'csrf-team',
      spaces: [project],
    };
    const createReceipt = deferred<unknown>();
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.create': () => createReceipt.promise,
      'agent.session.mode.update': { ok: true },
      'agent.session.prompt': { ok: true },
    } });
    setTeamDeploymentMeta();
    const rendered = renderApp(transport, teamSession, fakeTeamApi(teamSession));
    const user = userEvent.setup();
    await user.type(await screen.findByRole('textbox', { name: '问数问题' }), '卸载前的问题');
    await user.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.create')).toHaveLength(1));

    rendered.unmount();
    await act(async () => {
      createReceipt.resolve({ ok: true, session: { id: 'session-after-unmount' } });
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.mode.update')).toBe(false);
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.prompt')).toBe(false);
  });
});

function renderApp(transport: MockControlTransport, teamSession?: TeamSession, api?: TeamApi) {
  const content = (
    <ControlTransportProvider transport={transport}>
      <TooltipProvider><ZhangguiWenshuApp manifest={manifest as never} /></TooltipProvider>
    </ControlTransportProvider>
  );
  return render(teamSession ? <TeamProvider api={api ?? fakeTeamApi(teamSession)}>{content}</TeamProvider> : content);
}

function setTeamDeploymentMeta(): void {
  const meta = document.createElement('meta');
  meta.name = 'paw-deployment';
  meta.content = 'team';
  document.head.appendChild(meta);
}

function fakeTeamApi(teamSession: TeamSession, projectSessions: unknown[] = []): TeamApi {
  return {
    status: vi.fn().mockResolvedValue({ enabled: true, name: 'PAW Team' }),
    login: vi.fn(),
    me: vi.fn().mockResolvedValue(teamSession),
    logout: vi.fn().mockResolvedValue({ ok: true }),
    createProject: vi.fn(),
    listMembers: vi.fn().mockResolvedValue([]),
    listDirectory: vi.fn().mockResolvedValue([]),
    createMember: vi.fn(),
    setMemberStatus: vi.fn(),
    listProjectMembers: vi.fn().mockResolvedValue([]),
    addProjectMember: vi.fn(),
    removeProjectMember: vi.fn(),
    getProjectOverview: vi.fn(),
    getProjectPreview: vi.fn(),
    startProjectPreview: vi.fn(),
    stopProjectPreview: vi.fn(),
    listConnections: vi.fn().mockResolvedValue({ connections: [], grants: [] }),
    createConnectionWithToken: vi.fn(),
    startConnectionOAuth: vi.fn(),
    revokeConnection: vi.fn(),
    createConnectionGrant: vi.fn(),
    revokeConnectionGrant: vi.fn(),
    updateProjectBrief: vi.fn(),
    adoptSessionRequirements: vi.fn(),
    getSessionResourceSnapshot: vi.fn(),
    listProjectDrafts: vi.fn().mockResolvedValue([]),
    listProjectSessions: vi.fn().mockResolvedValue(projectSessions),
    getProjectDraftDiff: vi.fn(),
    publishProjectDraft: vi.fn(),
    integrateProjectDraft: vi.fn(),
    adoptProjectDraft: vi.fn(),
  } as unknown as TeamApi;
}

function teamExtensionSnapshot(spaceId: string, sessionId: string, overrides: Record<string, unknown> = {}): TeamSessionResourceSnapshot {
  const digest = 'team-extension-digest';
  return {
    sessionId,
    spaceId,
    selectionRevision: 1,
    publicationIds: ['publication-zhanggui'],
    items: [{
      publicationId: 'publication-zhanggui',
      packageId: manifest.packageId,
      version: manifest.version,
      digest,
      status: 'published',
      metadata: {
        displayName: manifest.label,
        description: '',
        publisher: 'PAW',
        source: { kind: 'bundled', label: 'Product bundle' },
        permissions: [],
        compatibility: {},
        security: {},
        installable: true,
        distribution: 'team_staged_source',
        version: manifest.version,
        extensionApp: {
          id: manifest.id,
          packageId: manifest.packageId,
          version: manifest.version,
          bindingSha256: manifest.bindingSha256,
          bindingCapability: `pawos.extension.binding.${manifest.bindingSha256.slice(0, 40)}`,
          skillRef: manifest.skillRef,
          skillSha256: manifest.skillSha256,
          verticalSuiteId: manifest.verticalSuiteId,
          verticalSuiteRevision: manifest.verticalSuiteRevision,
          sandbox: manifest.sandbox,
          packageDigest: digest,
        },
      },
      publishedByUserId: 'user-admin',
      publishedAtMs: 1,
      updatedAtMs: 1,
    }],
    createdAtMs: 1,
    ...overrides,
  };
}

function firstPromptTransport(sessionId: string, prompt: MockRouteHandler, routes: Partial<Record<ControlRequest['pathId'], MockRouteHandler>> = {}): MockControlTransport {
  return new MockControlTransport({ routes: {
    'agent.sessions.list': { ok: true, items: [] },
    'agent.sessions.create': { ok: true, session: { id: sessionId, title: '掌柜问数 · 问数', mode: 'assistant', status: 'idle', updatedAtMs: 1 } },
    'agent.session.mode.update': { ok: true },
    'extension.sandbox.experiment.run': {
      schemaVersion: 'rag-ime.extension-sandbox-experiment-receipt.v1', ok: true,
      sessionId, ownerAppId: manifest.id, candidateBindingSha256: manifest.bindingSha256,
      requestedDecision: 'skip', executed: false, executionStatus: 'skipped',
    },
    'agent.session.prompt': prompt,
    'agent.session.snapshot': { messages: [], liveEvents: [], lastSequence: 0, resumeToken: '', status: 'idle' },
    'agent.session.models': {},
    'agent.session.commands': {},
    'agent.tools.list': {},
    'agent.runtime.get': {},
    ...routes,
  } });
}

function promptRequests(transport: MockControlTransport): ControlRequest[] {
  return transport.requests.filter(({ request }) => request.pathId === 'agent.session.prompt').map(({ request }) => request);
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}

function requestFor(transport: MockControlTransport, pathId: ControlRequest['pathId']): ControlRequest {
  const request = transport.requests.find((call) => call.request.pathId === pathId)?.request;
  if (!request) throw new Error(`Missing request ${pathId}`);
  return request;
}
