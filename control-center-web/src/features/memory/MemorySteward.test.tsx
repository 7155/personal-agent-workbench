import { forwardRef, type ForwardedRef, type Key, type ReactNode } from 'react';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { agentSessionAddress, selectAgentProjection, useAgentLiveStore } from '@/features/agent/state/live-store';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport, type MockRouteHandler } from '@/test/mock-transport';
import { MemorySteward } from './MemorySteward';

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
        : <section data-testid="memory-steward-session">Session {props.recordId}</section>;
    },
  };
});

// Keep the real Session timeline and recovery actions; jsdom cannot measure
// virtual rows, so render those rows without replacing their behavior.
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
  sessionWorkspaceProps.mockClear();
  renderRealWorkspace.current = false;
  useAgentLiveStore.setState({ projections: {} });
  window.localStorage.clear();
});

describe('MemorySteward', () => {
  it('settles a late ambiguous first prompt only in its original transport projection', async () => {
    const user = userEvent.setup();
    const receipt = deferred<unknown>();
    const transportA = firstPromptTransport(() => receipt.promise);
    const transportB = new MockControlTransport();
    renderSteward(transportA);
    await user.type(await screen.findByRole('textbox', { name: '问记忆管家' }), '原连接的问题');
    await user.click(screen.getByRole('button', { name: '发送给记忆管家' }));
    await waitFor(() => expect(promptRequests(transportA)).toHaveLength(1));
    const request = promptRequests(transportA)[0];
    const sessionId = String(request.params?.sessionId);
    const clientMessageId = String((request.body as Record<string, unknown>).clientMessageId);
    const addressA = agentSessionAddress(transportA, sessionId);
    const addressB = agentSessionAddress(transportB, sessionId);
    useAgentLiveStore.getState().appendOptimistic(addressB, { clientMessageId, text: '另一个连接的问题', nowMs: 1 });
    const beforeB = selectAgentProjection(useAgentLiveStore.getState(), addressB);

    await act(async () => receipt.reject(new TypeError('fetch failed')));
    await waitFor(() => expect(Object.values(selectAgentProjection(useAgentLiveStore.getState(), addressA)!.messagesById)[0])
      .toMatchObject({ clientMessageId, status: 'failed', admissionState: 'ambiguous' }));
    expect(selectAgentProjection(useAgentLiveStore.getState(), addressB)).toBe(beforeB);
    expect(useAgentLiveStore.getState().projections[sessionId]).toBeUndefined();
    expect(promptRequests(transportA)).toHaveLength(1);
    expect(transportB.requests).toHaveLength(0);
  });

  it('keeps one read-only original input until the first prompt receipt confirms admission', async () => {
    const user = userEvent.setup();
    const receipt = deferred<unknown>();
    const transport = firstPromptTransport(() => receipt.promise);
    renderRealWorkspace.current = true;
    renderSteward(transport);
    await user.type(await screen.findByRole('textbox', { name: '问记忆管家' }), '保留正在发送的原始问题');
    await user.click(screen.getByRole('button', { name: '发送给记忆管家' }));

    await waitFor(() => expect(promptRequests(transport)).toHaveLength(1));
    expect(screen.getByRole('textbox', { name: '问记忆管家' })).toHaveValue('保留正在发送的原始问题');
    expect(screen.getByRole('textbox', { name: '问记忆管家' })).toHaveAttribute('readonly');
    expect(screen.getAllByRole('textbox')).toHaveLength(1);
    expect(screen.getByRole('button', { name: '最近有哪些 idea 还没完成？' })).toBeDisabled();
    expect(sessionWorkspaceProps).not.toHaveBeenCalled();

    await act(async () => receipt.resolve({ ok: true }));
    expect(await screen.findByText('用户问题：保留正在发送的原始问题')).toBeVisible();
    expect(screen.queryByRole('textbox', { name: '问记忆管家' })).not.toBeInTheDocument();
    expect(promptRequests(transport)).toHaveLength(1);
  });

  it.each(['ensure', 'mode'] as const)('preserves the original question after a %s preparation failure and waits for explicit retry', async (stage) => {
    const user = userEvent.setup();
    let attempts = 0;
    const transport = firstPromptTransport({ ok: true }, {
      [stage === 'ensure' ? 'agent.sessions.surface.ensure' : 'agent.session.mode.update']: () => {
        attempts += 1;
        if (attempts === 1) throw new Error('准备管家对话失败');
        return stage === 'ensure' ? ensuredSession() : { ok: true };
      },
    });
    renderSteward(transport);
    await user.type(await screen.findByRole('textbox', { name: '问记忆管家' }), '准备失败也不能丢失的问题');
    await user.click(screen.getByRole('button', { name: '发送给记忆管家' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('准备管家对话失败');
    expect(screen.getByRole('textbox', { name: '问记忆管家' })).toHaveValue('准备失败也不能丢失的问题');
    expect(promptRequests(transport)).toHaveLength(0);
    await user.click(screen.getByRole('button', { name: '发送给记忆管家' }));
    await screen.findByTestId('memory-steward-session');
    expect(promptRequests(transport)).toHaveLength(1);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.surface.ensure')).toHaveLength(stage === 'ensure' ? 2 : 1);
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.sessions.create')).toBe(false);
  });

  it.each(['conflict', 'cancelled'] as const)('preserves a %s prompt in the input and reuses the prepared Session only after explicit send', async (outcome) => {
    const user = userEvent.setup();
    let attempts = 0;
    const transport = firstPromptTransport((request: ControlRequest) => {
      attempts += 1;
      if (attempts > 1) return { ok: true };
      if (outcome === 'cancelled') return { accepted: false, cancelled: true, admissionCancelled: true };
      throw receiptFailure(request, 'conflict');
    });
    renderSteward(transport);
    await user.type(await screen.findByRole('textbox', { name: '问记忆管家' }), '明确未接收的问题');
    await user.click(screen.getByRole('button', { name: '发送给记忆管家' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(outcome === 'cancelled' ? '已取消' : '输入已保留');
    const input = screen.getByRole('textbox', { name: '问记忆管家' });
    expect(input).toHaveValue('明确未接收的问题');
    expect(sessionWorkspaceProps).not.toHaveBeenCalled();
    const original = promptRequests(transport)[0];
    await user.type(input, '，补充日期范围');
    await user.click(screen.getByRole('button', { name: '发送给记忆管家' }));
    await screen.findByTestId('memory-steward-session');
    const retried = promptRequests(transport)[1];
    expect(retried.params).toEqual(original.params);
    expect((retried.body as Record<string, unknown>).message).toContain('明确未接收的问题，补充日期范围');
    expect((retried.body as Record<string, unknown>).clientMessageId).not.toBe((original.body as Record<string, unknown>).clientMessageId);
    expect(retried.body).not.toHaveProperty('retryOfClientMessageId');
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.surface.ensure')).toHaveLength(1);
  });

  it('recovers a failed receipt through the shared same-Session retry with the original date and timeline context', async () => {
    const user = userEvent.setup();
    let attempts = 0;
    const transport = firstPromptTransport((request: ControlRequest) => {
      attempts += 1;
      if (attempts === 1) throw receiptFailure(request, 'failed');
      return { ok: true };
    });
    renderRealWorkspace.current = true;
    renderSteward(transport);
    await user.type(await screen.findByRole('textbox', { name: '问记忆管家' }), '恢复被拒绝的问题');
    await user.click(screen.getByRole('button', { name: '发送给记忆管家' }));
    const retry = await screen.findByRole('button', { name: '重试本轮' });
    expect(screen.getByText('用户问题：恢复被拒绝的问题')).toBeVisible();
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
    expect(String((retried.body as Record<string, unknown>).message)).toContain('当前日记日期：2026-08-31');
    expect(String((retried.body as Record<string, unknown>).message)).toContain('timeline-20260831');
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.surface.ensure')).toHaveLength(1);
  });

  it.each(['in_flight', 'unresolved'] as const)('keeps a %s receipt in the shared confirmation state without sending a duplicate', async (recoveryState) => {
    const user = userEvent.setup();
    const transport = firstPromptTransport((request: ControlRequest) => { throw receiptFailure(request, 'pending', recoveryState); });
    renderRealWorkspace.current = true;
    renderSteward(transport);
    await user.type(await screen.findByRole('textbox', { name: '问记忆管家' }), '接收状态未明的问题');
    await user.click(screen.getByRole('button', { name: '发送给记忆管家' }));
    expect(await screen.findByText('正在确认接收状态')).toBeVisible();
    expect(screen.getByText('用户问题：接收状态未明的问题')).toBeVisible();
    expect(screen.queryByRole('button', { name: '重试本轮' })).not.toBeInTheDocument();
    expect(screen.queryByText('本轮未完成')).not.toBeInTheDocument();
    expect(promptRequests(transport)).toHaveLength(1);
  });

  it('verifies an ambiguous admission with the same ID only on explicit retry and reconciles one durable question', async () => {
    const user = userEvent.setup();
    const transport = firstPromptTransport(() => { throw new TypeError('fetch failed'); });
    renderRealWorkspace.current = true;
    renderSteward(transport);
    await user.type(await screen.findByRole('textbox', { name: '问记忆管家' }), '网络回执丢失的问题');
    await user.click(screen.getByRole('button', { name: '发送给记忆管家' }));
    expect(await screen.findByText(/暂时无法确认是否已接收/)).toBeVisible();
    expect(screen.getByText('用户问题：网络回执丢失的问题')).toBeVisible();
    expect(promptRequests(transport)).toHaveLength(1);
    const original = promptRequests(transport)[0];
    const body = original.body as Record<string, unknown>;
    await user.click(screen.getByRole('button', { name: '重试本轮' }));
    await waitFor(() => expect(promptRequests(transport)).toHaveLength(2));
    expect(promptRequests(transport)[1]).toMatchObject({ params: original.params, body: { message: body.message, clientMessageId: body.clientMessageId } });
    expect(promptRequests(transport)[1].body).not.toHaveProperty('retryOfClientMessageId');
    const sessionId = 'session-memory-20260831';
    const address = agentSessionAddress(transport, sessionId);
    const local = Object.values(selectAgentProjection(useAgentLiveStore.getState(), address)!.messagesById)[0];
    act(() => useAgentLiveStore.getState().hydrateSnapshot(address, {
      messages: [{ ...local, id: 'durable-steward-question', status: 'completed', admissionState: undefined, clientMessageId: String(body.clientMessageId) }],
      liveEvents: [], lastSequence: 1, resumeToken: `${sessionId}:1`, status: 'idle',
    }));
    expect(screen.getAllByText('用户问题：网络回执丢失的问题')).toHaveLength(1);
    expect(screen.queryByText(/暂时无法确认是否已接收/)).not.toBeInTheDocument();
    expect(promptRequests(transport)).toHaveLength(2);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.surface.ensure')).toHaveLength(1);
  });

  it('keeps late preparation and its prompt bound to the old date while the new date remains editable', async () => {
    const user = userEvent.setup();
    const preparation = deferred<unknown>();
    const transport = firstPromptTransport({ ok: true }, { 'agent.sessions.surface.ensure': () => preparation.promise });
    const view = renderSteward(transport);
    await user.type(await screen.findByRole('textbox', { name: '问记忆管家' }), '前一天的问题');
    await user.click(screen.getByRole('button', { name: '发送给记忆管家' }));
    view.rerender(stewardView(transport, '2026-09-01'));
    await user.type(await screen.findByRole('textbox', { name: '问记忆管家' }), '新一天的草稿');
    expect(screen.getByRole('button', { name: '发送给记忆管家' })).toBeEnabled();
    await act(async () => preparation.resolve(ensuredSession()));
    await waitFor(() => expect(promptRequests(transport)).toHaveLength(1));
    expect(promptRequests(transport)[0].params).toEqual({ sessionId: 'session-memory-20260831' });
    expect(String((promptRequests(transport)[0].body as Record<string, unknown>).message)).toContain('当前日记日期：2026-08-31');
    expect(screen.getByRole('textbox', { name: '问记忆管家' })).toHaveValue('新一天的草稿');
    expect(screen.queryByTestId('memory-steward-session')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it.each(['accepted', 'failed'] as const)('does not let a late %s receipt unlock or replace another date\'s pending question', async (outcome) => {
    const user = userEvent.setup();
    const oldReceipt = deferred<unknown>();
    const nextReceipt = deferred<unknown>();
    const transport = firstPromptTransport((request: ControlRequest) => request.params?.sessionId === 'session-memory-20260831' ? oldReceipt.promise : nextReceipt.promise, {
      'agent.sessions.surface.ensure': (request: ControlRequest) => ensuredSession(String((request.body as Record<string, unknown>).surfaceKey).slice('journal-'.length)),
    });
    const view = renderSteward(transport);
    await user.type(await screen.findByRole('textbox', { name: '问记忆管家' }), '前一天的问题');
    await user.click(screen.getByRole('button', { name: '发送给记忆管家' }));
    await waitFor(() => expect(promptRequests(transport)).toHaveLength(1));
    view.rerender(stewardView(transport, '2026-09-01'));
    await user.type(await screen.findByRole('textbox', { name: '问记忆管家' }), '新一天正在发送的问题');
    await user.click(screen.getByRole('button', { name: '发送给记忆管家' }));
    await waitFor(() => expect(promptRequests(transport)).toHaveLength(2));
    await act(async () => outcome === 'accepted' ? oldReceipt.resolve({ ok: true }) : oldReceipt.reject(receiptFailure(promptRequests(transport)[0], 'failed')));
    expect(screen.getByRole('textbox', { name: '问记忆管家' })).toHaveValue('新一天正在发送的问题');
    expect(screen.getByRole('textbox', { name: '问记忆管家' })).toHaveAttribute('readonly');
    expect(screen.getByRole('button', { name: '发送给记忆管家' })).toBeDisabled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(sessionWorkspaceProps).not.toHaveBeenCalled();
    await act(async () => nextReceipt.resolve({ ok: true }));
    expect(await screen.findByTestId('memory-steward-session')).toHaveTextContent('session-memory-20260901');
  });

  it('starts a date-owned Pi Session in place and sends the real timeline boundary', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [] },
      'agent.sessions.surface.ensure': {
        ok: true,
        created: true,
        session: {
          id: 'session-memory-20260831',
          title: 'Memory 管家 · 2026-08-31',
          mode: 'assistant',
          status: 'idle',
          updatedAtMs: 1,
          surfaceKind: 'builtin_app',
          ownerAppId: 'memory',
          surfaceKey: 'journal-2026-08-31',
        },
      },
      'agent.session.mode.update': { ok: true },
      'agent.session.prompt': { ok: true },
    } });
    renderSteward(transport);

    await user.click(await screen.findByRole('button', { name: '最近有哪些 idea 还没完成？' }));
    await user.click(screen.getByRole('button', { name: '发送给记忆管家' }));

    expect(await screen.findByTestId('memory-steward-session')).toHaveTextContent('session-memory-20260831');
    expect(sessionWorkspaceProps).toHaveBeenLastCalledWith(expect.objectContaining({
      appearance: 'embedded',
      recordId: 'session-memory-20260831',
    }));
    expect(requestFor(transport, 'agent.sessions.surface.ensure').body).toMatchObject({
      surfaceKind: 'builtin_app',
      ownerAppId: 'memory',
      surfaceKey: 'journal-2026-08-31',
      executionMode: 'read_only',
    });
    expect(requestFor(transport, 'agent.session.mode.update').body).toMatchObject({
      executionMode: 'read_only',
      projectContextEnabled: false,
      piSkillsEnabled: true,
    });
    const prompt = String((requestFor(transport, 'agent.session.prompt').body as Record<string, unknown>).message);
    expect(prompt).toContain('2026-08-31');
    expect(prompt).toContain('timeline-20260831');
    expect(prompt).toContain('最近有哪些 idea 还没完成？');
    expect(window.localStorage.length).toBe(0);
  });

  it('restores only the Memory journal Session for the selected date', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': {
        ok: true,
        items: [{
          id: 'session-memory-existing',
          title: 'Memory 管家 · 2026-08-31',
          mode: 'assistant',
          status: 'idle',
          updatedAtMs: 8,
          surfaceKind: 'builtin_app',
          ownerAppId: 'memory',
          surfaceKey: 'journal-2026-08-31',
        }],
      },
    } });
    renderSteward(transport);

    expect(await screen.findByTestId('memory-steward-session')).toHaveTextContent('session-memory-existing');
    expect(requestFor(transport, 'agent.sessions.list').query).toMatchObject({
      surfaceKind: 'builtin_app',
      ownerAppId: 'memory',
      surfaceKey: 'journal-2026-08-31',
    });
  });

  it('keeps transport wording out of the personal Memory surface', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': () => { throw new Error('Failed to fetch'); },
    } });
    renderSteward(transport);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('暂时没有读到这一天的管家对话，仍可重新开始。');
    expect(alert).not.toHaveTextContent('Failed to fetch');
  });
});

function stewardView(transport: MockControlTransport, date = '2026-08-31') {
  return (
    <ControlTransportProvider transport={transport}>
      <TooltipProvider><MemorySteward date={date} timelineId={`timeline-${date.replaceAll('-', '')}`} /></TooltipProvider>
    </ControlTransportProvider>
  );
}

function renderSteward(transport: MockControlTransport) {
  return render(stewardView(transport));
}

function ensuredSession(date = '2026-08-31') {
  return { ok: true, created: true, session: {
    id: `session-memory-${date.replaceAll('-', '')}`, title: `Memory 管家 · ${date}`,
    mode: 'assistant', status: 'idle', updatedAtMs: 1,
    surfaceKind: 'builtin_app', ownerAppId: 'memory', surfaceKey: `journal-${date}`,
  } };
}

function firstPromptTransport(prompt: MockRouteHandler, routes: Partial<Record<ControlRequest['pathId'], MockRouteHandler>> = {}) {
  return new MockControlTransport({ routes: {
    'agent.sessions.list': { ok: true, items: [] },
    'agent.sessions.surface.ensure': ensuredSession(),
    'agent.session.mode.update': { ok: true },
    'agent.session.prompt': prompt,
    'agent.session.snapshot': { messages: [], liveEvents: [], lastSequence: 0, resumeToken: '', status: 'idle' },
    'agent.session.models': {}, 'agent.session.commands': {}, 'agent.tools.list': {}, 'agent.runtime.get': {},
    ...routes,
  } });
}

function receiptFailure(request: ControlRequest, state: 'pending' | 'failed' | 'conflict', recoveryState?: 'in_flight' | 'unresolved') {
  return Object.assign(new Error('本次发送未被确认'), {
    status: state === 'failed' ? 400 : 409,
    payload: {
      code: `AGENT_COMMAND_${state.toUpperCase()}`,
      commandReceipt: { state, clientMessageId: (request.body as Record<string, unknown>).clientMessageId, ...(recoveryState ? { recoveryState } : {}) },
    },
  });
}

function promptRequests(transport: MockControlTransport): ControlRequest[] {
  return transport.requests.filter(({ request }) => request.pathId === 'agent.session.prompt').map(({ request }) => request);
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function requestFor(transport: MockControlTransport, pathId: ControlRequest['pathId']): ControlRequest {
  const request = transport.requests.find((call) => call.request.pathId === pathId)?.request;
  if (!request) throw new Error(`Missing request ${pathId}`);
  return request;
}
