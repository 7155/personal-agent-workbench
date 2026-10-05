import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport, type MockRouteHandler } from '@/test/mock-transport';
import type { SessionSummary } from '@/features/agent/types';
import type { ControlRequest } from '@/platform/transport';
import { PawPrimaryAssistantHome } from './PawPrimaryAssistantHome';
import type { PrimaryAssistantSource } from './agent-workspace-loader';
import { useAgentLiveSession } from '@/features/agent/runtime/use-agent-live-session';
import { agentSessionAddress, selectAgentProjection, useAgentLiveStore } from '@/features/agent/state/live-store';

afterEach(() => {
  cleanup();
  for (const id of Object.keys(useAgentLiveStore.getState().projections)) useAgentLiveStore.getState().clear(id);
});
const primary: SessionSummary = { id: 'primary', title: '我的助手', status: 'idle', mode: 'assistant', roleId: '', roleVersion: '', roleBookRevisionId: '', workspaceRoots: [], updatedAtMs: 1, executionMode: 'read_only', metadata: { primaryAssistant: true } };
const task: SessionSummary = { ...primary, id: 'task', title: '检查项目', executionMode: 'workspace_managed', metadata: { primaryTask: true, sourceSessionId: 'primary' } };
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
function setup(routes: Partial<Record<ControlRequest['pathId'], MockRouteHandler>> = {}, initialSource?: PrimaryAssistantSource) {
  const transport = new MockControlTransport({ routes: { 'agent.primary.ensure': { ok: true, session: primary, tasks: [] },
    'agent.session.snapshot': (request: ControlRequest) => taskSnapshot(String(request.params?.sessionId)), ...routes } });
  const onOpen = vi.fn();
  const tree = <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ControlTransportProvider transport={transport}><PawPrimaryAssistantHome onOpen={onOpen} onAdvanced={vi.fn()} projectRoots={['/work/project']} initialSource={initialSource} /></ControlTransportProvider></QueryClientProvider>;
  return { transport, onOpen, ...render(tree) };
}
describe('primary assistant home', () => {
  it.each([
    { name: 'objective', objective: 'x'.repeat(4001), criteria: '', field: '和我的助手聊聊' },
    { name: 'criteria count', objective: '检查项目', criteria: Array(21).fill('通过').join('\n'), field: '完成标准' },
    { name: 'criteria rendered length', objective: '检查项目', criteria: `${'x'.repeat(1000)}\n${'y'.repeat(1000)}`, field: '完成标准' },
  ])('blocks invalid $name without losing text or sending authorization', async ({ objective, criteria, field }) => {
    const { transport, onOpen } = setup({ 'agent.primary.tasks.create': { ok: true, session: task } });
    await waitFor(() => expect(screen.getByRole('button', { name: /打开对话/ })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '交给助手做' }));
    const input = screen.getByRole('textbox', { name: '和我的助手聊聊' });
    fireEvent.change(input, { target: { value: objective } });
    fireEvent.change(screen.getByRole('textbox', { name: '完成标准' }), { target: { value: criteria } });
    fireEvent.change(screen.getByRole('textbox', { name: '本次工作目录' }), { target: { value: '/work/project' } });
    fireEvent.click(screen.getByRole('checkbox'));
    expect(screen.getByRole('button', { name: '授权并开始任务' })).toBeDisabled();
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(screen.getByRole('textbox', { name: field })).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('textbox', { name: field })).toHaveFocus();
    expect(input).toHaveValue(objective);
    expect(screen.getByRole('textbox', { name: '完成标准' })).toHaveValue(criteria);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.primary.tasks.create')).toHaveLength(0);
    expect(onOpen).not.toHaveBeenCalled();
  });
  it('admits exact code-point and rendered-criteria boundaries', async () => {
    const { transport } = setup({ 'agent.primary.tasks.create': { ok: true, session: task } });
    await waitFor(() => expect(screen.getByRole('button', { name: /打开对话/ })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '交给助手做' }));
    const objective = '🙂'.repeat(4000);
    const criteria = [...Array(19).fill('x'), 'y'.repeat(1962)];
    fireEvent.change(screen.getByRole('textbox', { name: '和我的助手聊聊' }), { target: { value: objective } });
    fireEvent.change(screen.getByRole('textbox', { name: '完成标准' }), { target: { value: criteria.join('\n') } });
    fireEvent.change(screen.getByRole('textbox', { name: '本次工作目录' }), { target: { value: '/work/project' } });
    fireEvent.click(screen.getByRole('checkbox'));
    const submit = screen.getByRole('button', { name: '授权并开始任务' });
    expect(submit).toBeEnabled(); fireEvent.click(submit);
    await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'agent.primary.tasks.create')).toHaveLength(1));
    expect(transport.requests.find(({ request }) => request.pathId === 'agent.primary.tasks.create')?.request.body).toMatchObject({ objective, acceptanceCriteria: criteria });
  });
  it('does not apply task-objective limits to ordinary discussion', async () => {
    const { onOpen } = setup();
    await waitFor(() => expect(screen.getByRole('button', { name: /打开对话/ })).toBeEnabled());
    const message = 'x'.repeat(4001);
    fireEvent.change(screen.getByRole('textbox', { name: '和我的助手聊聊' }), { target: { value: message } });
    fireEvent.click(screen.getByRole('button', { name: '发送给我的助手' }));
    expect(onOpen).toHaveBeenCalledWith(primary, expect.objectContaining({ message }));
  });
  it('keeps IME confirmation Enter and legacy keyCode 229 out of submission', async () => {
    const { onOpen } = setup();
    await waitFor(() => expect(screen.getByRole('button', { name: /打开对话/ })).toBeEnabled());
    const input = screen.getByRole('textbox', { name: '和我的助手聊聊' });
    fireEvent.change(input, { target: { value: '输入中文' } });
    fireEvent.compositionStart(input);
    fireEvent.keyDown(input, { key: 'Enter', isComposing: false });
    fireEvent.compositionEnd(input);
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 229, isComposing: false });
    expect(onOpen).not.toHaveBeenCalled();
    expect(input).toHaveValue('输入中文');
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 13 });
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
  it('removes the previous project task rows immediately while the next project loads', async () => {
    const nextProject = deferred<unknown>();
    const { onOpen } = setup({ 'agent.primary.ensure': (request: ControlRequest) =>
      (request.body as { workspaceRoots?: string[] }).workspaceRoots?.length
        ? nextProject.promise
        : { ok: true, session: primary, tasks: [task] } });
    await screen.findByRole('button', { name: /检查项目/ });
    await waitFor(() => expect(screen.getByRole('combobox', { name: '讨论项目' })).toBeEnabled());
    fireEvent.change(screen.getByRole('combobox', { name: '讨论项目' }), { target: { value: '/work/project' } });
    expect(screen.queryByRole('button', { name: /检查项目/ })).not.toBeInTheDocument();
    expect(onOpen).not.toHaveBeenCalled();
    await act(async () => nextProject.resolve({ ok: true, session: { ...primary, id: 'other-primary', workspaceRoots: ['/work/project'] }, tasks: [] }));
    expect(screen.queryByRole('button', { name: /检查项目/ })).not.toBeInTheDocument();
  });
  it('preserves the visible conversation while gating admission during refresh', async () => {
    const refresh = deferred<unknown>();
    let reads = 0;
    setup({ 'agent.primary.ensure': () => ++reads === 1
      ? { ok: true, session: primary, tasks: [] }
      : refresh.promise });
    const open = await screen.findByRole('button', { name: /打开对话/ });
    expect(open).toBeEnabled();
    fireEvent(window, new Event('focus'));
    await waitFor(() => expect(open).toBeDisabled());
    expect(screen.getByRole('button', { name: /打开对话/ })).toBe(open);
    await act(async () => refresh.resolve({ ok: true, session: primary, tasks: [] }));
    await waitFor(() => expect(open).toBeEnabled());
  });
  it('requires fresh scope confirmation after transport replacement and drops the old message cutoff', async () => {
    const original = new MockControlTransport({ routes: { 'agent.primary.ensure': { ok: true, session: primary, tasks: [] } } });
    const next = new MockControlTransport({ routes: { 'agent.primary.ensure': { ok: true, session: primary, tasks: [] }, 'agent.primary.tasks.create': { ok: true, session: task } } });
    const source = { sessionId: primary.id, workspaceRoots: ['/work/project'], messageId: 'old-server-cutoff' };
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tree = (transport: MockControlTransport) => <QueryClientProvider client={client}><ControlTransportProvider transport={transport}><PawPrimaryAssistantHome initialSource={source} onOpen={vi.fn()} onAdvanced={vi.fn()} /></ControlTransportProvider></QueryClientProvider>;
    const view = render(tree(original));
    await screen.findByRole('button', { name: /打开对话/ });
    fireEvent.change(screen.getByRole('textbox', { name: '和我的助手聊聊' }), { target: { value: '检查目标' } });
    fireEvent.click(screen.getByRole('button', { name: '交给助手做' }));
    fireEvent.click(screen.getByRole('checkbox'));
    view.rerender(tree(next));
    await waitFor(() => expect(screen.getByRole('button', { name: /打开对话/ })).toBeEnabled());
    expect(screen.getByRole('textbox', { name: '和我的助手聊聊' })).toHaveValue('检查目标');
    expect(screen.getByRole('checkbox')).not.toBeChecked();
    expect(screen.getByRole('button', { name: '授权并开始任务' })).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: '授权并开始任务' }));
    await waitFor(() => expect(next.requests.some(({ request }) => request.pathId === 'agent.primary.tasks.create')).toBe(true));
    expect(next.requests.find(({ request }) => request.pathId === 'agent.primary.tasks.create')?.request.body).not.toHaveProperty('sourceMessageId');
  });
  it('explains disabled execution and holds navigation until the task receipt arrives', async () => {
    const response = deferred<unknown>();
    const { onOpen } = setup({ 'agent.primary.ensure': { ok: true, session: primary, tasks: [task] }, 'agent.primary.tasks.create': () => response.promise });
    await screen.findByRole('button', { name: /打开对话/ });
    fireEvent.change(screen.getByRole('textbox', { name: '和我的助手聊聊' }), { target: { value: '检查项目' } });
    fireEvent.click(screen.getByRole('button', { name: '交给助手做' }));
    expect(screen.getByRole('status')).toHaveTextContent('先选择本次工作目录');
    expect(screen.getByRole('textbox', { name: '和我的助手聊聊' })).toHaveFocus();
    fireEvent.change(screen.getByRole('textbox', { name: '本次工作目录' }), { target: { value: '/work/project' } });
    expect(screen.getByRole('status')).toHaveTextContent('确认目录权限后');
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: '授权并开始任务' }));
    expect(screen.getByRole('status')).toHaveTextContent('正在确认任务');
    const previous = screen.getByRole('button', { name: /检查项目.*查看进度/ });
    expect(previous).toBeDisabled(); fireEvent.click(previous);
    expect(onOpen).not.toHaveBeenCalled();
    await act(async () => response.resolve({ ok: true, session: task }));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
  it('keeps composing and Shift-Enter local instead of dispatching', async () => {
    const { onOpen } = setup();
    await screen.findByRole('button', { name: /打开对话/ });
    const input = screen.getByRole('textbox', { name: '和我的助手聊聊' });
    fireEvent.change(input, { target: { value: '还在输入' } });
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true });
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true });
    expect(onOpen).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
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
  it('projects authoritative goal changes immediately without refetching the task directory', async () => {
    const { transport } = setup({
      'agent.primary.ensure': { ok: true, session: primary, tasks: [{ ...task, goal: taskGoal('active') }] },
      'agent.session.snapshot': taskSnapshot('task', 'active'),
    });
    await screen.findByRole('button', { name: /检查项目.*任务未完成/ });
    await waitFor(() => expect(transport.subscriptionCalls.filter(call => call.request.pathId === 'agent.session.events')).toHaveLength(1));
    act(() => { transport.emit('agent.session.events', taskEvent('workflow_changed', { goal: taskGoal('completed', 2) })); });
    await screen.findByRole('button', { name: /检查项目.*已完成/ });
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.primary.ensure')).toHaveLength(1);
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.prompt')).toBe(false);
  });

  it('shares one live owner with a task detail observer and ignores a stale busy directory row', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.primary.ensure': { ok: true, session: primary, tasks: [{ ...task, status: 'busy', goal: taskGoal('active') }] },
      'agent.session.snapshot': taskSnapshot('task', 'active'),
    } });
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ControlTransportProvider transport={transport}>
      <PawPrimaryAssistantHome onOpen={vi.fn()} onAdvanced={vi.fn()} />
      <TaskDetailProjection transport={transport} />
    </ControlTransportProvider></QueryClientProvider>);
    await screen.findByRole('button', { name: /检查项目.*任务未完成/ });
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.snapshot')).toHaveLength(1);
    act(() => { transport.emit('agent.session.events', taskEvent('workflow_changed', { goal: taskGoal('completed', 2) })); });
    expect(await screen.findByRole('button', { name: /检查项目.*已完成/ })).toBeVisible();
    expect(screen.getByTestId('detail-goal')).toHaveTextContent('completed:2');
    expect(transport.activeSubscriptionCount()).toBe(1);
  });

  it.each(['busy', 'completed'] as const)('marks a disconnected %s task as last-known progress until the shared owner recovers', async initialStatus => {
    const recovered = deferred<unknown>();
    const goalStatus = initialStatus === 'busy' ? 'active' : 'completed';
    const statusLabel = initialStatus === 'busy' ? '进行中' : '已完成';
    let snapshots = 0;
    const { transport } = setup({
      'agent.primary.ensure': { ok: true, session: primary, tasks: [{ ...task, status: initialStatus === 'busy' ? 'busy' : 'idle', goal: taskGoal(goalStatus) }] },
      'agent.session.snapshot': () => ++snapshots === 1
        ? { ...taskSnapshot('task', goalStatus), status: initialStatus === 'busy' ? 'busy' : 'idle',
          runtimeQuiescent: initialStatus !== 'busy', lastSequence: 1, resumeToken: 'task:1' }
        : recovered.promise,
    });
    await screen.findByRole('button', { name: new RegExp(`检查项目.*${statusLabel}`) });
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));
    act(() => { transport.fail('agent.session.events', new Error('connection lost')); });
    expect(screen.getByRole('button', { name: /检查项目/ })).toHaveTextContent(`正在重新同步 · 上次状态：${statusLabel}`);
    expect(screen.getByRole('button', { name: /检查项目/ })).toBeEnabled();
    await waitFor(() => expect(snapshots).toBe(2), { timeout: 5_000 });
    await act(async () => recovered.resolve({ ...taskSnapshot('task', 'completed'), lastSequence: 2,
      resumeToken: 'task:2', goal: taskGoal('completed', 2) }));
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /检查项目/ })).toHaveTextContent('已完成');
      expect(screen.getByRole('button', { name: /检查项目/ })).not.toHaveTextContent('上次状态');
    });
    expect(transport.activeSubscriptionCount()).toBe(1);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.primary.ensure')).toHaveLength(1);
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.prompt')).toBe(false);
  });

  it('ignores a retired task observer after unmounting and opening the same id on another transport', async () => {
    const first = setup({
      'agent.primary.ensure': { ok: true, session: primary, tasks: [{ ...task, status: 'busy', goal: taskGoal('active') }] },
      'agent.session.snapshot': { ...taskSnapshot('task', 'active'), status: 'busy', runtimeQuiescent: false },
    });
    const subscribe = first.transport.subscribe.bind(first.transport);
    let lateFailure: (() => void) | undefined;
    vi.spyOn(first.transport, 'subscribe').mockImplementation((request, observer) => {
      lateFailure = () => observer.error?.(new Error('retired observer'));
      return subscribe(request, observer);
    });
    await waitFor(() => expect(first.transport.activeSubscriptionCount()).toBe(1));
    first.unmount();
    expect(first.transport.activeSubscriptionCount()).toBe(0);
    const next = setup({
      'agent.primary.ensure': { ok: true, session: primary, tasks: [{ ...task, goal: taskGoal('completed') }] },
      'agent.session.snapshot': taskSnapshot('task', 'completed'),
    });
    await waitFor(() => expect(next.transport.activeSubscriptionCount()).toBe(1));
    expect(lateFailure).toBeTypeOf('function');
    act(() => { lateFailure?.(); });
    expect(screen.getByRole('button', { name: /检查项目/ })).toHaveTextContent('已完成');
    expect(screen.getByRole('button', { name: /检查项目/ })).not.toHaveTextContent('上次状态');
    expect(next.transport.requests.filter(({ request }) => request.pathId === 'agent.session.snapshot')).toHaveLength(1);
    expect(next.transport.activeSubscriptionCount()).toBe(1);
  });

  it('keeps a stopped or finished turn distinct from a completed goal', async () => {
    let sequence = 0;
    const { transport } = setup({
      'agent.primary.ensure': { ok: true, session: primary, tasks: [{ ...task, goal: taskGoal('active') }] },
      'agent.session.snapshot': () => ({ ...taskSnapshot('task', 'active'), lastSequence: sequence }),
    });
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(1));
    act(() => { sequence = 1; transport.emit('agent.session.events', taskEvent('turn_completed', { status: 'aborted', aborted: true })); });
    await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.snapshot')).toHaveLength(2));
    expect(screen.getByRole('button', { name: /检查项目/ })).toHaveTextContent('任务未完成');
    expect(screen.getByRole('button', { name: /检查项目/ })).not.toHaveTextContent('已完成');
  });

  it('does not open a stream for every active idle task when expanding the directory', async () => {
    const tasks = Array.from({ length: 12 }, (_, index) => ({ ...task, id: `bounded-${index}`, title: `任务 ${index}`,
      status: index === 10 ? 'busy' : 'idle', goal: taskGoal('active') }));
    const { transport } = setup({
      'agent.primary.ensure': { ok: true, session: primary, tasks },
      'agent.session.snapshot': (request: ControlRequest) => ({
        ...taskSnapshot(String(request.params?.sessionId), 'active'), status: request.params?.sessionId === 'bounded-10' ? 'busy' : 'idle',
      }),
    });
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(5));
    fireEvent.click(screen.getByRole('button', { name: '查看全部 12 个任务' }));
    expect(screen.getByRole('button', { name: /任务 11/ })).toBeVisible();
    expect(transport.activeSubscriptionCount()).toBe(5);
    fireEvent.focus(screen.getByRole('button', { name: /任务 6/ }));
    await waitFor(() => expect(transport.activeSubscriptionCount()).toBe(6));
  });
});

function taskGoal(status: 'active' | 'completed', revision = 1, sessionId = 'task') {
  return { schemaVersion: 'rag-ime.agent-goal.v1', sessionId, configured: true,
    goalId: 'goal', revision, status, objective: '检查项目', successCriteria: '' };
}
function taskSnapshot(sessionId: string, status?: 'active' | 'completed') {
  return { sessionId, snapshotScope: 'recent', partial: true, status: 'idle', runtimeQuiescent: true,
    messages: [], liveEvents: [], lastSequence: 0, resumeToken: `${sessionId}:0`,
    ...(status ? { goal: taskGoal(status, 1, sessionId) } : {}) };
}
function taskEvent(eventType: string, payload: Record<string, unknown>) {
  return { schemaVersion: 'rag-ime.agent-event.v1', eventId: 'task:1', sessionId: 'task', turnId: 'turn',
    sequence: 1, createdAtMs: Date.now(), eventType, payload, resumeToken: 'task:1' };
}
/** The same production owner/projection used by an open detail surface. */
function TaskDetailProjection({ transport }: { transport: MockControlTransport }) {
  useAgentLiveSession({ sessionId: 'task', transport, snapshotView: 'recent' });
  const goal = useAgentLiveStore(state => selectAgentProjection(state, agentSessionAddress(transport, 'task'))?.goal);
  return <output data-testid="detail-goal">{goal?.status}:{goal?.revision}</output>;
}
