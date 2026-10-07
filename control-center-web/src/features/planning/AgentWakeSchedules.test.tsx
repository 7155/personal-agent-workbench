import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import userEvent from '@testing-library/user-event';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { StubControlTransport } from '@/test/stub-control-transport';
import { AgentWakeSchedules } from './AgentWakeSchedules';

afterEach(cleanup);

describe('AgentWakeSchedules target catalog feedback', () => {
  it('returns focus to the PR entry that actually opened the cancelled dialog', async () => {
    const user = userEvent.setup();
    const transport = new StubControlTransport('native', {
      'agent.sessions.list': sessionListResponse(), 'agent.roles.list': roleListResponse(),
      'agent.wakeSchedules.list': scheduleListResponse(),
    });
    renderSchedules(transport);
    const trigger = screen.getByRole('button', { name: '跟进 GitHub PR' });
    await waitFor(() => expect(trigger).toBeEnabled());
    await user.click(trigger);
    expect(await screen.findByRole('dialog')).toBeVisible();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(transport.requests.every(request => !request.body)).toBe(true);
  });
  it('does not imply a saved schedule in the empty inactive-scheduler state', async () => {
    const transport = new StubControlTransport('native', {
      'agent.sessions.list': sessionListResponse(), 'agent.roles.list': roleListResponse(),
      'agent.wakeSchedules.list': { ok: true, schedulerActive: false, items: [] },
    });
    renderSchedules(transport);
    expect(await screen.findByText('尚未添加安排；本机 Agent 服务运行后才能按时执行。')).toBeInTheDocument();
    expect(screen.queryByText(/安排已保存/)).not.toBeInTheDocument();
    expect(await screen.findByText('还没有定时安排')).toBeInTheDocument();
    expect(transport.requests.every(request => !request.body)).toBe(true);
  });
  it('retains the persisted-schedule notice when the scheduler is inactive', async () => {
    const transport = new StubControlTransport('native', {
      'agent.sessions.list': sessionListResponse(), 'agent.roles.list': roleListResponse(),
      'agent.wakeSchedules.list': { ...scheduleListResponse(), schedulerActive: false },
    });
    renderSchedules(transport);
    expect(await screen.findByText('安排已保存；本机 Agent 服务运行后才能按时执行。')).toBeInTheDocument();
    expect(screen.queryByText(/尚未添加安排/)).not.toBeInTheDocument();
  });
  it('keeps existing schedules visible while reading available targets', async () => {
    const sessions = deferred<unknown>();
    const transport = new StubControlTransport('native', {
      'agent.sessions.list': () => sessions.promise,
      'agent.roles.list': roleListResponse(),
      'agent.wakeSchedules.list': scheduleListResponse(),
    });

    renderSchedules(transport);

    expect(screen.getByText('正在读取可安排的对话与伙伴')).toBeInTheDocument();
    expect(await screen.findByText('已有安排')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '添加安排' })).toBeDisabled();

    await act(async () => {
      sessions.resolve(sessionListResponse());
    });

    await waitFor(() => expect(screen.getByRole('button', { name: '添加安排' })).toBeEnabled());
    expect(screen.queryByText('正在读取可安排的对话与伙伴')).not.toBeInTheDocument();
  });

  it('explains a target catalog failure, keeps schedules visible, and retries the real request', async () => {
    let unavailable = true;
    const transport = new StubControlTransport('native', {
      'agent.sessions.list': () => {
        if (unavailable) throw new Error('catalog unavailable');
        return sessionListResponse();
      },
      'agent.roles.list': roleListResponse(),
      'agent.wakeSchedules.list': scheduleListResponse(),
    });

    renderSchedules(transport);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('暂时无法读取可安排的对话与伙伴');
    expect(alert).toHaveTextContent('已有安排仍可查看和管理；重新读取后才能添加新的安排。');
    expect(await screen.findByText('已有安排')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '添加安排' })).toBeDisabled();

    unavailable = false;
    fireEvent.click(screen.getByRole('button', { name: '重新读取对象' }));

    await waitFor(() => expect(screen.getByRole('button', { name: '添加安排' })).toBeEnabled());
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(transport.requests.filter((request) => request.pathId === 'agent.sessions.list')).toHaveLength(2);
  });

  it('does not expose a stored conversation id when its title is no longer available', async () => {
    const transport = new StubControlTransport('native', {
      'agent.sessions.list': { ok: true, items: [] },
      'agent.roles.list': roleListResponse(),
      'agent.wakeSchedules.list': scheduleListResponse(),
    });

    renderSchedules(transport);

    expect(await screen.findByText('对话：原对话已不在列表中', { exact: false })).toBeInTheDocument();
    expect(screen.queryByText(/session-planning/)).not.toBeInTheDocument();
  });
});

function renderSchedules(transport: StubControlTransport) {
  const queryClient = new QueryClient({
    defaultOptions: {
      mutations: { retry: false },
      queries: { retry: false },
    },
  });

  return render(
    <TooltipProvider delayDuration={0}>
      <ControlTransportProvider transport={transport}>
        <QueryClientProvider client={queryClient}>
          <AgentWakeSchedules tasks={[]} />
        </QueryClientProvider>
      </ControlTransportProvider>
    </TooltipProvider>,
  );
}

function sessionListResponse() {
  return {
    ok: true,
    items: [{
      id: 'session-planning',
      title: '规划对话',
      status: 'idle',
      updatedAtMs: 1_784_006_400_000,
    }],
  };
}

function roleListResponse() {
  return { ok: true, items: [] };
}

function scheduleListResponse() {
  return {
    ok: true,
    schedulerActive: true,
    items: [{
      id: 'wake-existing',
      title: '已有安排',
      instruction: '继续检查结果',
      targetType: 'session',
      targetSessionId: 'session-planning',
      status: 'scheduled',
      runCount: 0,
      maxRuns: 1,
      recurrenceKind: 'once',
      nextWakeAtMs: 1_784_006_400_000,
      latestRun: {},
    }],
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}
