import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport, type MockRouteHandler } from '@/test/mock-transport';
import type { SessionSummary } from '@/features/agent/types';
import type { ControlRequest } from '@/platform/transport';
import { PawPrimaryAssistantHome } from './PawPrimaryAssistantHome';
import type { PrimaryAssistantSource } from './agent-workspace-loader';

afterEach(cleanup);
const primary: SessionSummary = { id: 'primary', title: '我的助手', status: 'idle', mode: 'assistant', roleId: '', roleVersion: '', roleBookRevisionId: '', workspaceRoots: [], updatedAtMs: 1, executionMode: 'read_only', metadata: { primaryAssistant: true } };
const task: SessionSummary = { ...primary, id: 'task', title: '检查项目', executionMode: 'workspace_managed', metadata: { primaryTask: true, sourceSessionId: 'primary' } };
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
function setup(routes: Partial<Record<ControlRequest['pathId'], MockRouteHandler>> = {}, initialSource?: PrimaryAssistantSource) {
  const transport = new MockControlTransport({ routes: { 'agent.primary.ensure': { ok: true, session: primary, tasks: [] }, ...routes } });
  const onOpen = vi.fn();
  const tree = <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ControlTransportProvider transport={transport}><PawPrimaryAssistantHome onOpen={onOpen} onAdvanced={vi.fn()} projectRoots={['/work/project']} initialSource={initialSource} /></ControlTransportProvider></QueryClientProvider>;
  return { transport, onOpen, ...render(tree) };
}
describe('primary assistant home', () => {
  it('keeps the source project and exact public message cutoff when handing a discussion to execution', async () => {
    const { transport } = setup({
      'agent.primary.ensure': { ok: true, session: { ...primary, id: 'project-primary', workspaceRoots: ['/work/project', '/work/shared'] }, tasks: [] },
      'agent.primary.tasks.create': { ok: true, session: task },
    }, { sessionId: 'project-primary', workspaceRoots: ['/work/project', '/work/shared'], messageId: 'public-plan-message' });
    await screen.findByRole('button', { name: /打开对话/ });
    expect(transport.requests.find(({ request }) => request.pathId === 'agent.primary.ensure')?.request.body).toEqual({ workspaceRoots: ['/work/project', '/work/shared'] });
    fireEvent.change(screen.getByRole('textbox', { name: '和我的助手聊聊' }), { target: { value: '按这个设计检查项目入口' } });
    fireEvent.click(screen.getByRole('button', { name: '交给助手做' }));
    expect(screen.getByRole('textbox', { name: '本次工作目录' })).toHaveValue('/work/project');
    expect(screen.getByRole('list', { name: '本次授权目录' })).toHaveTextContent('/work/project');
    expect(screen.getByRole('list', { name: '本次授权目录' })).toHaveTextContent('/work/shared');
    fireEvent.click(screen.getByRole('checkbox')); fireEvent.click(screen.getByRole('button', { name: '授权并开始任务' }));
    await waitFor(() => expect(transport.requests.find(({ request }) => request.pathId === 'agent.primary.tasks.create')?.request.body).toMatchObject({
      sourceSessionId: 'project-primary', sourceMessageId: 'public-plan-message', workspaceRoots: ['/work/project', '/work/shared'],
    }));
  });
  it('opens one stable read-only discussion without a Room or provider call', async () => {
    const { transport, onOpen } = setup();
    await waitFor(() => expect(screen.getByRole('button', { name: /打开对话/ })).toBeEnabled());
    expect(screen.getByRole('button', { name: '聊一聊' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.change(screen.getByRole('textbox', { name: '和我的助手聊聊' }), { target: { value: '先聊聊设计' } });
    fireEvent.click(screen.getByRole('button', { name: '发送给我的助手' }));
    expect(onOpen).toHaveBeenCalledWith(primary, { message: '先聊聊设计', clientMessageId: expect.any(String) });
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.primary.ensure')).toHaveLength(1);
    expect(transport.requests.some(({ request }) => ['agent.session.prompt', 'agent.sessions.create', 'agent.rooms.create'].includes(request.pathId))).toBe(false);
  });
  it('requires a concrete execution scope, deduplicates clicks, and preserves the request identity at handoff', async () => {
    const response = deferred<unknown>();
    const { transport, onOpen } = setup({ 'agent.primary.tasks.create': () => response.promise });
    await screen.findByRole('button', { name: /打开对话/ });
    fireEvent.change(screen.getByRole('textbox', { name: '和我的助手聊聊' }), { target: { value: '检查项目' } });
    fireEvent.click(screen.getByRole('button', { name: '交给助手做' }));
    const start = screen.getByRole('button', { name: '授权并开始任务' });
    expect(start).toBeDisabled();
    fireEvent.change(screen.getByRole('textbox', { name: '本次工作目录' }), { target: { value: '/work/project' } });
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(start); fireEvent.click(start);
    const creates = transport.requests.filter(({ request }) => request.pathId === 'agent.primary.tasks.create');
    expect(creates).toHaveLength(1);
    expect(creates[0].request.body).toMatchObject({ sourceSessionId: primary.id, objective: '检查项目', workspaceRoots: ['/work/project'], workspaceScopeConfirmation: 'APPROVE_WORKSPACE_SCOPE' });
    await act(async () => response.resolve({ ok: true, session: task }));
    expect(onOpen).toHaveBeenCalledWith(task, { message: '检查项目', clientMessageId: (creates[0].request.body as Record<string, unknown>).clientRequestId });
  });
  it('keeps an uncertain task draft and retries the exact client request', async () => {
    let count = 0;
    const { transport } = setup({ 'agent.primary.tasks.create': () => { if (!count++) throw new Error('connection lost'); return { ok: true, session: task }; } });
    await screen.findByRole('button', { name: /打开对话/ });
    fireEvent.change(screen.getByRole('textbox', { name: '和我的助手聊聊' }), { target: { value: '检查项目' } });
    fireEvent.click(screen.getByRole('button', { name: '交给助手做' }));
    fireEvent.change(screen.getByRole('textbox', { name: '本次工作目录' }), { target: { value: '/work/project' } });
    fireEvent.click(screen.getByRole('checkbox')); fireEvent.click(screen.getByRole('button', { name: '授权并开始任务' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('草稿已保留');
    expect(screen.getByRole('textbox', { name: '和我的助手聊聊' })).toHaveValue('检查项目');
    fireEvent.click(screen.getByRole('button', { name: '授权并开始任务' }));
    await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'agent.primary.tasks.create')).toHaveLength(2));
    const writes = transport.requests.filter(({ request }) => request.pathId === 'agent.primary.tasks.create');
    expect(writes[0].request.body).toEqual(writes[1].request.body);
  });
  it('retains a draft through a disconnected ensure and never falls back to new-session creation', async () => {
    const { transport } = setup({ 'agent.primary.ensure': () => { throw new Error('offline'); } });
    fireEvent.change(screen.getByRole('textbox', { name: '和我的助手聊聊' }), { target: { value: '我的草稿' } });
    await screen.findByRole('alert');
    fireEvent.click(screen.getByRole('button', { name: '重新连接' }));
    await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'agent.primary.ensure')).toHaveLength(2));
    expect(screen.getByRole('textbox', { name: '和我的助手聊聊' })).toHaveValue('我的草稿');
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.sessions.create')).toBe(false);
  });
  it('selects an isolated project discussion without losing text', async () => {
    const { transport } = setup();
    await waitFor(() => expect(screen.getByRole('combobox', { name: '讨论项目' })).toBeEnabled());
    fireEvent.change(screen.getByRole('textbox', { name: '和我的助手聊聊' }), { target: { value: '这个项目的想法' } });
    fireEvent.change(screen.getByRole('combobox', { name: '讨论项目' }), { target: { value: '/work/project' } });
    await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'agent.primary.ensure')).toHaveLength(2));
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.primary.ensure')[1].request.body).toEqual({ workspaceRoots: ['/work/project'] });
    expect(screen.getByRole('textbox', { name: '和我的助手聊聊' })).toHaveValue('这个项目的想法');
  });
  it('keeps older tasks reachable and reads completion only from goal state', async () => {
    const tasks = Array.from({ length: 5 }, (_, index) => ({ ...task, id: `task-${index}`, title: `任务 ${index}`, lastTerminalTurnId: 'last-turn', goal: { goalId: 'goal', revision: 1, status: index === 4 ? 'completed' : 'active', objective: 'work', successCriteria: '' } }));
    setup({ 'agent.primary.ensure': { ok: true, session: primary, tasks } });
    await screen.findByRole('button', { name: '查看全部 5 个任务' });
    expect(screen.queryByRole('button', { name: /任务 4/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /任务 0/ })).toHaveTextContent('任务未完成');
    fireEvent.click(screen.getByRole('button', { name: '查看全部 5 个任务' }));
    expect(screen.getByRole('button', { name: /任务 4/ })).toHaveTextContent('已完成');
  });
  it('does not send into the previous project when the newly selected project is disconnected', async () => {
    const { onOpen } = setup({ 'agent.primary.ensure': (request: ControlRequest) => {
      if ((request.body as { workspaceRoots?: string[] }).workspaceRoots?.length) throw new Error('offline project');
      return { ok: true, session: primary, tasks: [] };
    } });
    await waitFor(() => expect(screen.getByRole('combobox', { name: '讨论项目' })).toBeEnabled());
    fireEvent.change(screen.getByRole('textbox', { name: '和我的助手聊聊' }), { target: { value: '项目里的私有背景' } });
    fireEvent.change(screen.getByRole('combobox', { name: '讨论项目' }), { target: { value: '/work/project' } });
    await screen.findByRole('alert');
    expect(screen.getByRole('button', { name: '发送给我的助手' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: /打开对话/ })).not.toBeInTheDocument();
    expect(onOpen).not.toHaveBeenCalled();
  });
  it('refreshes unfinished tasks from terminal events without starting another work loop', async () => {
    let done = false;
    const { transport } = setup({ 'agent.primary.ensure': () => ({ ok: true, session: primary, tasks: [{ ...task, goal: { goalId: 'goal', revision: 1, status: done ? 'completed' : 'active', objective: task.title, successCriteria: '' } }] }) });
    await screen.findByRole('button', { name: /检查项目.*任务未完成/ });
    await waitFor(() => expect(transport.subscriptionCalls.filter(call => call.request.pathId === 'agent.session.events')).toHaveLength(1));
    done = true;
    act(() => { transport.emit('agent.session.events', { schemaVersion: 'rag-ime.agent-event.v1', eventId: 'task:1', sessionId: 'task', turnId: 'turn', sequence: 1, createdAtMs: Date.now(), eventType: 'turn_completed', payload: { status: 'completed' }, resumeToken: 'task:1' }); });
    await screen.findByRole('button', { name: /检查项目.*已完成/ });
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.primary.ensure')).toHaveLength(2);
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.prompt')).toBe(false);
  });
});
