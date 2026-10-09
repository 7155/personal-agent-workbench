import { forwardRef, type ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { createPreviewTransport } from '@/app/preview-control-transport';
import { TooltipProvider } from '@/components/primitives';
import { parseAgentEvent } from '@/contracts/validators';
import type { AgentMessageV1 } from '@/contracts/generated/agent-message.v1';
import { agentProjectionKey, agentSessionAddress, useAgentLiveStore } from '@/features/agent/state/live-store';
import { recoveryScope } from '@/features/semantic-workspace/workspace-recovery';
import { CONTROL_ROUTES, controlRoute, type ControlPathId } from '@/platform/routes';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport, type MockRouteHandler } from '@/test/mock-transport';
import { PawAgentApp } from './PawAgentApp';

// jsdom has no layout. The Home, Session, reducers and Stop UI are production;
// catalog responses and the explicitly controlled native event/ACK contract
// below are mock fixtures, not evidence of a real Agent or Provider run.
vi.mock('react-virtuoso', () => ({ Virtuoso: forwardRef(function Virtualizer({ data, itemContent, scrollerRef, components, context }: {
  data: string[]; itemContent: (index: number, item: string) => ReactNode;
  scrollerRef?: (node: HTMLElement | null) => void;
  components?: { Header?: (props: { context?: unknown }) => ReactNode; Footer?: () => ReactNode }; context?: unknown;
}, _ref) { const Header = components?.Header; const Footer = components?.Footer;
  return <div ref={scrollerRef}>{Header ? <Header context={context} /> : null}{data.map((id, index) => <div key={id}>{itemContent(index, id)}</div>)}{Footer ? <Footer /> : null}</div>;
}) }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear(); for (const id of Object.keys(useAgentLiveStore.getState().projections)) useAgentLiveStore.getState().clear(id); });

it('creates an independent Session by keyboard, stops its original turn, and reopens its retained draft without creating an inline task', async () => {
  const user = userEvent.setup();
  const { transport, originalSessionId, stoppedTurns, emit, complete, currentTurn } = independentSessionTransport();
  const view = render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <ControlTransportProvider transport={transport}><TooltipProvider><PawAgentApp initialRoute="/agent" /></TooltipProvider></ControlTransportProvider>
  </QueryClientProvider>);
  const home = () => within(view.container.querySelector<HTMLElement>('.an-home-root')!);
  const composer = () => within(view.container.querySelector<HTMLElement>('.paw-session-workspace .agent-composer')!);
  const workspace = () => within(view.container.querySelector<HTMLElement>('.paw-session-workspace')!);
  const firstDraft = '检查独立 Session 入口\n保留原始请求身份';
  const homeDraft = await within(view.container).findByRole('textbox', { name: '描述你想完成的工作' });
  await user.type(homeDraft, '检查独立 Session 入口');
  await user.keyboard('{Shift>}{Enter}{/Shift}保留原始请求身份');
  expect(homeDraft).toHaveValue(firstDraft);
  await user.click(home().getByRole('radio', { name: 'Session' }));
  await user.keyboard('{ArrowRight}');
  expect(home().getByRole('radio', { name: 'Room' })).toBeChecked();
  expect(home().getByRole('radio', { name: 'Room' })).toHaveFocus();
  expect(homeDraft).toHaveValue(firstDraft);
  await user.keyboard('{ArrowLeft}');
  expect(home().getByRole('radio', { name: 'Session' })).toBeChecked();
  expect(home().getByRole('radio', { name: 'Session' })).toHaveFocus();
  expect(transport.requests.some(({ request }) => ['agent.sessions.create', 'agent.rooms.create', 'agent.session.prompt'].includes(request.pathId))).toBe(false);
  await user.click(home().getByRole('button', { name: /^权限 ·/ }));
  await user.click(await within(document.body).findByRole('menuitemradio', { name: /^工作区托管（沙箱）/ }));
  await waitFor(() => expect(home().getByRole('button', { name: '开始 Session' })).toBeEnabled());
  await user.click(homeDraft);
  await user.keyboard('{Enter}');
  await waitFor(() => expect(composer().getByRole('button', { name: '停止本轮' })).toBeVisible());
  const create = transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.create');
  expect(create).toHaveLength(1);
  expect(create[0]!.request.body).toMatchObject({ mode: 'coordinator', executionMode: 'workspace_managed',
    toolProfileVersion: 'control-center-v1', workspaceRoots: ['/Users/example/Projects/personal-agent-workbench'],
    workspaceScopeConfirmation: 'APPROVE_WORKSPACE_SCOPE' });
  const firstPrompt = transport.requests.find(({ request }) => request.pathId === 'agent.session.prompt')!.request;
  expect(firstPrompt.params).toEqual({ sessionId: originalSessionId });
  expect(firstPrompt.body).toMatchObject({ message: firstDraft, attachments: [], clientMessageId: expect.any(String) });
  const originalTurnId = currentTurn();
  expect(originalTurnId).toBe(`${originalSessionId}:turn:${(firstPrompt.body as Record<string, unknown>).clientMessageId}`);
  const projection = () => useAgentLiveStore.getState().projections[agentProjectionKey(agentSessionAddress(transport, originalSessionId))]!;
  expect(projection().turnsById[originalTurnId]?.status).toBe('running');
  const retainedDraft = '停止后保留这段草稿\n继续核对结果';
  const editable = composer().getByRole('textbox', { name: '消息' });
  await user.type(editable, '停止后保留这段草稿');
  await user.keyboard('{Shift>}{Enter}{/Shift}继续核对结果');
  expect(editable).toHaveValue(retainedDraft);
  expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.prompt')).toHaveLength(1);
  const stop = composer().getByRole('button', { name: '停止本轮' });
  await user.click(stop);
  await waitFor(() => expect(composer().queryByRole('button', { name: '停止本轮' })).not.toBeInTheDocument());
  expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.abort').map(({ request }) => request)).toEqual([
    expect.objectContaining({ params: { sessionId: originalSessionId }, body: {} }),
  ]);
  expect(stoppedTurns).toEqual([originalTurnId]);
  expect(projection().turnsById[originalTurnId]?.status).toBe('aborted');
  expect(editable).toHaveValue(retainedDraft);
  const storageKey = recoveryScope(transport, `session:${originalSessionId}`);
  expect(storageKey).not.toBe('');
  expect(JSON.parse(localStorage.getItem(storageKey)!)).toMatchObject({ draft: retainedDraft });
  expect(workspace().queryByText('当前 Session 的受控结果')).not.toBeInTheDocument();
  await user.click(within(view.container).getByRole('button', { name: '工作台选项' }));
  await user.click(within(document.body).getByRole('menuitem', { name: '新建 Session 或 Room' }));
  await within(view.container).findByRole('textbox', { name: '描述你想完成的工作' });
  await user.click(within(view.container).getByRole('button', { name: '打开工作记录' }));
  const records = within(view.container).getByRole('complementary', { name: 'Agent 工作记录' });
  const original = await within(records).findByRole('button', { name: /^检查独立 Session 入口/ });
  await user.click(original);
  const restored = await within(view.container).findByRole('textbox', { name: '消息' });
  expect(restored).toHaveValue(retainedDraft);
  expect(projection().turnsById[originalTurnId]?.status).toBe('aborted');
  expect(transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.create')).toHaveLength(1);
  await user.click(restored);
  await user.keyboard('{Enter}');
  await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.prompt')).toHaveLength(2));
  const nextTurnId = currentTurn();
  expect(nextTurnId).not.toBe(originalTurnId);
  // A late terminal for the stopped original turn must not settle its successor.
  act(() => emit(originalTurnId, 'turn_completed', { status: 'aborted', aborted: true }));
  expect(projection().turnsById[nextTurnId]?.status).toBe('running');
  expect(projection().status).toBe('busy');
  act(() => complete('当前 Session 的受控结果'));
  await workspace().findByText('当前 Session 的受控结果', { selector: 'p' });
  expect(projection().turnsById[originalTurnId]?.status).toBe('aborted');
  expect(projection().turnsById[nextTurnId]?.status).toBe('completed');
  const prompts = transport.requests.filter(({ request }) => request.pathId === 'agent.session.prompt');
  expect(prompts).toHaveLength(2);
  expect(prompts[1]!.request).toMatchObject({ params: { sessionId: originalSessionId }, body: { message: retainedDraft, clientMessageId: expect.any(String) } });
  expect((prompts[1]!.request.body as Record<string, unknown>).clientMessageId).not.toBe((firstPrompt.body as Record<string, unknown>).clientMessageId);
  expect(transport.requests.some(({ request }) => ['agent.primary.ensure', 'agent.primary.tasks.create', 'agent.rooms.create'].includes(request.pathId))).toBe(false);
  expect(view.container.querySelector('.paw-primary-home')).toBeNull();
});

function independentSessionTransport() {
  const preview = createPreviewTransport();
  const originalSessionId = 'session-persona-1';
  let session: Record<string, unknown> | undefined;
  let turnId = '';
  let sequence = 0;
  const stoppedTurns: string[] = [];
  const messages: AgentMessageV1[] = [];
  const events: ReturnType<typeof parseAgentEvent>[] = [];
  const fallback = Object.fromEntries((Object.keys(CONTROL_ROUTES) as ControlPathId[])
    .filter(pathId => !controlRoute(pathId).subscription)
    .map(pathId => [pathId, (request: ControlRequest) => preview.request(request)])) as Partial<Record<ControlPathId, MockRouteHandler>>;
  const transport = new MockControlTransport({ routes: {
    ...fallback,
    'agent.sessions.create': async (request: ControlRequest) => {
      const response = await preview.request<{ ok: boolean; session: Record<string, unknown> }>(request);
      session = { ...response.session, ...(request.body as Record<string, unknown>), id: originalSessionId };
      return { ...response, session };
    },
    'agent.sessions.list': async (request: ControlRequest) => {
      const page = await preview.request<{ items: Record<string, unknown>[] }>(request);
      return { ...page, items: page.items.map(item => item.id === session?.id ? session : item) };
    },
    'agent.session.snapshot': () => ({ messages, liveEvents: events, lastSequence: sequence,
      resumeToken: `${originalSessionId}:${sequence}`, status: turnId ? 'busy' : 'idle' }),
    'agent.session.prompt': (request: ControlRequest) => {
      const body = request.body as { clientMessageId: string; message: string };
      turnId = `${originalSessionId}:turn:${body.clientMessageId}`;
      const message = publicMessage('user', body.message, body.clientMessageId);
      messages.push(message);
      emit(turnId, 'message_completed', { message });
      emit(turnId, 'status_changed', { status: 'busy' });
      return { ok: true, accepted: true, sessionId: originalSessionId, turnId, clientMessageId: body.clientMessageId };
    },
    'agent.session.abort': () => {
      const stopped = turnId;
      stoppedTurns.push(stopped);
      turnId = '';
      emit(stopped, 'turn_completed', { status: 'aborted', aborted: true });
      return { ok: true, sessionId: originalSessionId, runtimeReceipt: { turnId: stopped },
        backgroundJobs: { drained: true, pendingJobIds: [] } };
    },
  } });
  // Recovery intentionally excludes anonymous mock backends. Bind this fixture
  // like the production HTTP transport instead of borrowing another connection.
  Object.defineProperty(transport, 'connectionIdentity', { value: 'primary-flow-independent-session-fixture' });
  function emit(originalTurnId: string, eventType: 'message_completed' | 'status_changed' | 'turn_completed', payload: Record<string, unknown>) {
    const event = parseAgentEvent({ schemaVersion: 'rag-ime.agent-event.v1', eventId: `${originalSessionId}:${++sequence}`,
      sessionId: originalSessionId, turnId: originalTurnId, sequence, createdAtMs: Date.now(), eventType, payload,
      resumeToken: `${originalSessionId}:${sequence}` });
    events.push(event);
    transport.emit('agent.session.events', event);
  }
  function publicMessage(role: 'user' | 'assistant', text: string, clientMessageId?: string): AgentMessageV1 {
    return { schemaVersion: 'rag-ime.agent-message.v1', id: `${turnId}:${role}`, sessionId: originalSessionId,
      turnId, role, status: 'completed', ...(clientMessageId ? { clientMessageId } : {}),
      blocks: [{ id: `${turnId}:${role}:text`, type: 'text', status: 'completed', presentationKind: 'markdown', data: { text } }],
      attachments: [], citations: [], createdAtMs: Date.now(), completedAtMs: Date.now() };
  }
  return { transport, originalSessionId, stoppedTurns, emit, currentTurn: () => turnId, complete(text: string) {
    const message = publicMessage('assistant', text);
    messages.push(message);
    emit(turnId, 'message_completed', { message });
    const completed = turnId;
    turnId = '';
    emit(completed, 'turn_completed', { status: 'completed' });
  } };
}
