import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider, useControlTransport } from '@/app/control-transport';
import type { AgentBackgroundJobV1 } from '@/contracts/generated/agent-background-job.v1';
import { MockControlTransport } from '@/test/mock-transport';
import { agentProjectionKey, agentSessionAddress, selectAgentProjection, useAgentLiveStore } from '../state/live-store';
import { AgentBackgroundJobsView } from './AgentBackgroundJobsView';

const sessionId = 'session-background-job';
const runningJob = backgroundJob('running', { label: '构建项目' });

beforeEach(() => {
  useAgentLiveStore.setState({ projections: {} });
});

afterEach(() => {
  cleanup();
  useAgentLiveStore.setState({ projections: {} });
});

describe('AgentBackgroundJobsView', () => {
  it('keeps lists and log caches separate for same-ID jobs on two transports', async () => {
    const jobA = backgroundJob('running', { label: '甲的构建' });
    const jobB = backgroundJob('running', { label: '乙的构建' });
    const a = new MockControlTransport({ routes: {
      'agent.session.backgroundJobs.list': jobList([jobA]),
      'agent.session.backgroundJob.logs': logResponse(jobA, '甲的输出'),
    } });
    const b = new MockControlTransport({ routes: {
      'agent.session.backgroundJobs.list': jobList([jobB]),
      'agent.session.backgroundJob.logs': logResponse(jobB, '乙的输出'),
    } });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    render(<QueryClientProvider client={client}>
      <ControlTransportProvider transport={a}><section aria-label="任务甲"><LiveJobs /></section></ControlTransportProvider>
      <ControlTransportProvider transport={b}><section aria-label="任务乙"><LiveJobs /></section></ControlTransportProvider>
    </QueryClientProvider>);
    const regionA = within(screen.getByRole('region', { name: '任务甲' }));
    const regionB = within(screen.getByRole('region', { name: '任务乙' }));
    const user = userEvent.setup();
    await user.click(await regionA.findByRole('button', { name: /甲的构建/ }));
    await user.click(await regionB.findByRole('button', { name: /乙的构建/ }));
    expect(await regionA.findByText('甲的输出')).toBeVisible();
    expect(await regionB.findByText('乙的输出')).toBeVisible();
    expect(regionA.queryByText('乙的输出')).not.toBeInTheDocument();
    expect(regionB.queryByText('甲的输出')).not.toBeInTheDocument();
  });

  it('applies a delayed cancel to its original transport after the provider switches', async () => {
    const jobA = backgroundJob('running', { label: '甲的构建' });
    const jobB = backgroundJob('running', { label: '乙的构建' });
    const cancelled = { ...jobA, status: 'cancelling' as const, updatedAtMs: 200, cancelRequestedAtMs: 200 };
    let finishCancel!: (receipt: unknown) => void;
    const pending = new Promise<unknown>((resolve) => { finishCancel = resolve; });
    const a = new MockControlTransport({ routes: {
      'agent.session.backgroundJobs.list': jobList([jobA]),
      'agent.session.backgroundJob.logs': logResponse(jobA),
      'agent.session.backgroundJob.cancel': () => pending,
    } });
    const b = new MockControlTransport({ routes: {
      'agent.session.backgroundJobs.list': jobList([jobB]),
      'agent.session.backgroundJob.logs': logResponse(jobB),
    } });
    const addressA = agentSessionAddress(a, sessionId);
    const addressB = agentSessionAddress(b, sessionId);
    for (const [address, job] of [[addressA, jobA], [addressB, jobB]] as const) {
      useAgentLiveStore.getState().hydrateSnapshot(address, {
        messages: [], liveEvents: [], lastSequence: 4, resumeToken: `${sessionId}:4`, backgroundJobs: [job],
      });
    }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    const element = (transport: MockControlTransport) => (
      <QueryClientProvider client={client}><ControlTransportProvider transport={transport}><LiveJobs /></ControlTransportProvider></QueryClientProvider>
    );
    const view = render(element(a));
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /甲的构建/ }));
    await user.click(screen.getByRole('button', { name: '停止任务' }));
    await user.click(screen.getByRole('button', { name: '确认停止' }));
    view.rerender(element(b));
    expect(await screen.findByRole('button', { name: /乙的构建/ })).toBeVisible();
    await act(async () => finishCancel({
      schemaVersion: 'rag-ime.agent-background-job-cancel-receipt.v1', ok: true,
      summary: '已请求停止甲任务', alreadyTerminal: false, job: cancelled,
      cancelReceipt: { jobId: jobA.jobId, requestedAtMs: 200, status: 'cancelling' },
    }));
    await waitFor(() => expect(selectAgentProjection(useAgentLiveStore.getState(), addressA)?.backgroundJobsById[jobA.jobId]?.status).toBe('cancelling'));
    expect(selectAgentProjection(useAgentLiveStore.getState(), addressB)?.backgroundJobsById[jobB.jobId]?.status).toBe('running');
    expect(client.getQueryData(['agent', 'background-jobs', agentProjectionKey(addressA)])).toMatchObject({ items: [{ status: 'cancelling' }] });
    expect(client.getQueryData(['agent', 'background-jobs', agentProjectionKey(addressB)])).toMatchObject({ items: [{ label: '乙的构建', status: 'running' }] });
    expect(screen.queryByText('已请求停止甲任务')).not.toBeInTheDocument();
    expect(b.requests.some(({ request }) => request.pathId === 'agent.session.backgroundJob.cancel')).toBe(false);
  });

  it('renders every server-owned lifecycle state and refreshes the list', async () => {
    const statuses: AgentBackgroundJobV1['status'][] = [
      'queued',
      'running',
      'cancelling',
      'completed',
      'failed',
      'cancelled',
      'orphaned',
    ];
    const jobs = statuses.map((status, index) => backgroundJob(status, {
      jobId: `bg_${String(index + 1).padStart(32, '0')}`,
      label: `任务-${status}`,
      updatedAtMs: 500 - index,
    }));
    const transport = new MockControlTransport({
      routes: {
        'agent.session.backgroundJobs.list': jobList(jobs),
      },
    });
    const user = userEvent.setup();
    renderJobs(transport, []);

    expect(await screen.findByText('7 个任务 · 最多显示 100 个')).toBeVisible();
    expect(screen.getByText(/等待启动 ·/)).toBeVisible();
    expect(screen.getByText(/运行中 ·/)).toBeVisible();
    expect(screen.getByText(/正在停止 ·/)).toBeVisible();
    expect(screen.getByText(/已完成 ·/)).toBeVisible();
    expect(screen.getByText(/失败 ·/)).toBeVisible();
    expect(screen.getByText(/已停止 ·/)).toBeVisible();
    expect(screen.getByText(/宿主已断开 ·/)).toBeVisible();

    const requestsBeforeRefresh = transport.requests.filter((call) => (
      call.request.pathId === 'agent.session.backgroundJobs.list'
    )).length;
    await user.click(screen.getByRole('button', { name: '刷新后台任务' }));
    await waitFor(() => {
      expect(transport.requests.filter((call) => (
        call.request.pathId === 'agent.session.backgroundJobs.list'
      )).length).toBeGreaterThan(requestsBeforeRefresh);
    });
  });

  it('projects a newly started live job before the next list poll', async () => {
    const transport = new MockControlTransport({
      routes: {
        'agent.session.backgroundJobs.list': jobList([]),
      },
    });
    useAgentLiveStore.getState().hydrateSnapshot(agentSessionAddress(transport, sessionId), {
      messages: [],
      liveEvents: [],
      lastSequence: 4,
      resumeToken: `${sessionId}:4`,
      backgroundJobs: [],
    });
    renderJobs(transport);
    expect(await screen.findByText(/后台运行命令后会显示在这里/)).toBeVisible();

    act(() => {
      useAgentLiveStore.getState().hydrateSnapshot(agentSessionAddress(transport, sessionId), {
        messages: [],
        liveEvents: [],
        lastSequence: 5,
        resumeToken: `${sessionId}:5`,
        backgroundJobs: [runningJob],
      });
    });

    expect(await screen.findByRole('button', { name: /构建项目/ })).toBeVisible();
  });

  it('keeps the live snapshot but does not list or poll while its PAWOS window is inactive', async () => {
    const transport = new MockControlTransport({
      routes: {
        'agent.session.backgroundJobs.list': jobList([runningJob]),
      },
    });

    renderJobs(transport, [runningJob], undefined, false);
    expect(await screen.findByRole('button', { name: /构建项目/ })).toBeVisible();
    await act(async () => { await new Promise((resolve) => window.setTimeout(resolve, 0)); });

    expect(transport.requests.filter((call) => (
      call.request.pathId === 'agent.session.backgroundJobs.list'
    ))).toHaveLength(0);
  });

  it('requests only the bounded log tail and discloses Runtime truncation', async () => {
    const largeJob = backgroundJob('running', {
      label: '大型构建',
      outputBytes: 400_000,
      logStartCursor: 200_000,
      logTruncated: true,
    });
    const expectedCursor = 400_000 - 131_072;
    const transport = new MockControlTransport({
      routes: {
        'agent.session.backgroundJobs.list': jobList([largeJob]),
        'agent.session.backgroundJob.logs': {
          schemaVersion: 'rag-ime.agent-background-job-log.v1',
          ok: true,
          jobId: largeJob.jobId,
          sessionId,
          cursor: expectedCursor,
          nextCursor: 400_000,
          logStartCursor: 200_000,
          truncatedBeforeCursor: false,
          hasMore: false,
          text: 'latest bounded output',
        },
      },
    });
    const user = userEvent.setup();
    renderJobs(transport);

    await user.click(await screen.findByRole('button', { name: /大型构建/ }));

    expect(await screen.findByText('latest bounded output')).toBeVisible();
    expect(screen.getByText(/较早日志已被 Runtime 截断/)).toBeVisible();
    expect(transport.requests.find((call) => (
      call.request.pathId === 'agent.session.backgroundJob.logs'
    ))?.request.query).toEqual({
      cursor: expectedCursor,
      limitBytes: 131_072,
    });
  });

  it('opens the exact existing run only after the user asks to view it', async () => {
    const transport = new MockControlTransport({
      routes: {
        'agent.session.backgroundJobs.list': jobList([runningJob]),
        'agent.session.backgroundJob.logs': logResponse(runningJob),
      },
    });
    const onOpenJob = vi.fn();
    const user = userEvent.setup();
    renderJobs(transport, [runningJob], onOpenJob);

    expect(onOpenJob).not.toHaveBeenCalled();
    await user.click(await screen.findByRole('button', { name: /构建项目/ }));
    expect(onOpenJob).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '在窗口查看' }));

    expect(onOpenJob).toHaveBeenCalledTimes(1);
    expect(onOpenJob).toHaveBeenCalledWith(runningJob);
  });

  it('projects the authoritative cancel receipt without waiting for SSE', async () => {
    const cancellingJob = backgroundJob('cancelling', {
      label: runningJob.label,
      jobId: runningJob.jobId,
      updatedAtMs: 200,
      cancelRequestedAtMs: 200,
      error: 'control_center_requested',
    });
    const transport = new MockControlTransport({
      routes: {
        'agent.session.backgroundJobs.list': jobList([runningJob]),
        'agent.session.backgroundJob.logs': logResponse(runningJob),
        'agent.session.backgroundJob.cancel': {
          schemaVersion: 'rag-ime.agent-background-job-cancel-receipt.v1',
          ok: true,
          summary: '已请求停止后台任务',
          alreadyTerminal: false,
          job: cancellingJob,
          cancelReceipt: {
            jobId: runningJob.jobId,
            requestedAtMs: 200,
            status: 'cancelling',
          },
        },
      },
    });
    const user = userEvent.setup();
    renderJobs(transport);

    await user.click(await screen.findByRole('button', { name: /构建项目/ }));
    await user.click(screen.getByRole('button', { name: '停止任务' }));
    await user.click(screen.getByRole('button', { name: '确认停止' }));

    await waitFor(() => {
      expect(selectAgentProjection(useAgentLiveStore.getState(), agentSessionAddress(transport, sessionId))?.backgroundJobsById[runningJob.jobId])
        .toMatchObject({ status: 'cancelling', cancelRequestedAtMs: 200 });
    });
    expect(screen.getByText(/正在停止 ·/)).toBeVisible();
    expect(screen.getByText('已请求停止后台任务')).toBeVisible();
    expect(screen.queryByRole('button', { name: /^(停止任务|确认停止|正在停止…)$/ })).not.toBeInTheDocument();
    const cancellationCalls = transport.requests.filter((call) => (
      call.request.pathId === 'agent.session.backgroundJob.cancel'
    ));
    expect(cancellationCalls).toHaveLength(1);
    expect(cancellationCalls[0]?.request).toMatchObject({
      params: { sessionId, jobId: runningJob.jobId },
      body: { reason: 'control_center_requested' },
    });
    const listCallsBeforeRefresh = transport.requests.filter((call) => (
      call.request.pathId === 'agent.session.backgroundJobs.list'
    )).length;
    await user.click(screen.getByRole('button', { name: '刷新后台任务' }));
    await waitFor(() => {
      expect(transport.requests.filter((call) => (
        call.request.pathId === 'agent.session.backgroundJobs.list'
      )).length).toBeGreaterThan(listCallsBeforeRefresh);
    });
    expect(screen.getByText(/正在停止 ·/)).toBeVisible();
    expect(screen.queryByRole('button', { name: '停止任务' })).not.toBeInTheDocument();
  });

  it('keeps Room-bound cancellation out of Session controls while preserving status and logs', async () => {
    const roomBoundJob = backgroundJob('running', {
      label: 'Room 构建',
      causalMetadata: {
        ...runningJob.causalMetadata,
        roomBound: true,
      },
    });
    const transport = new MockControlTransport({
      routes: {
        'agent.session.backgroundJobs.list': jobList([roomBoundJob]),
        'agent.session.backgroundJob.logs': logResponse(roomBoundJob, 'room-owned output'),
      },
    });
    const user = userEvent.setup();
    renderJobs(transport);

    await user.click(await screen.findByRole('button', { name: /Room 构建/ }));

    expect(await screen.findByText('room-owned output')).toBeVisible();
    expect(screen.getByText(/运行中 ·/)).toBeVisible();
    expect(screen.getByText('此任务属于 Room；请在对应 Room 中停止。')).toBeVisible();
    expect(screen.queryByRole('button', { name: /^(停止任务|确认停止|正在停止…)$/ }))
      .not.toBeInTheDocument();
    expect(transport.requests.filter((call) => (
      call.request.pathId === 'agent.session.backgroundJob.cancel'
    ))).toHaveLength(0);
  });

  it('keeps the server state and exposes a retry when cancellation fails', async () => {
    const transport = new MockControlTransport({
      routes: {
        'agent.session.backgroundJobs.list': jobList([runningJob]),
        'agent.session.backgroundJob.logs': logResponse(runningJob),
        'agent.session.backgroundJob.cancel': () => {
          throw new Error('Runtime 拒绝停止请求');
        },
      },
    });
    const user = userEvent.setup();
    renderJobs(transport);

    await user.click(await screen.findByRole('button', { name: /构建项目/ }));
    await user.click(screen.getByRole('button', { name: '停止任务' }));
    await user.click(screen.getByRole('button', { name: '确认停止' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Runtime 拒绝停止请求');
    expect(screen.getByText(/运行中 ·/)).toBeVisible();
    expect(screen.getByRole('button', { name: '确认停止' })).toBeEnabled();
    expect(transport.requests.filter((call) => (
      call.request.pathId === 'agent.session.backgroundJob.cancel'
    ))).toHaveLength(1);
  });

  it('shows list failure, retries, and lands on the guided empty state', async () => {
    let attempts = 0;
    const transport = new MockControlTransport({
      routes: {
        'agent.session.backgroundJobs.list': () => {
          attempts += 1;
          if (attempts === 1) throw new Error('list unavailable');
          return jobList([]);
        },
      },
    });
    const user = userEvent.setup();
    renderJobs(transport, []);

    expect(await screen.findByRole('alert')).toHaveTextContent('后台任务暂时不可用');
    await user.click(screen.getByRole('button', { name: '重新读取' }));

    expect(await screen.findByText(/后台运行命令后会显示在这里/)).toBeVisible();
    expect(attempts).toBe(2);
  });

  it('retries a failed bounded log read without changing job status', async () => {
    let attempts = 0;
    const transport = new MockControlTransport({
      routes: {
        'agent.session.backgroundJobs.list': jobList([runningJob]),
        'agent.session.backgroundJob.logs': () => {
          attempts += 1;
          if (attempts === 1) throw new Error('log unavailable');
          return logResponse(runningJob, 'recovered output');
        },
      },
    });
    const user = userEvent.setup();
    renderJobs(transport);

    await user.click(await screen.findByRole('button', { name: /构建项目/ }));
    expect(await screen.findByText('日志暂时不可用')).toBeVisible();
    await user.click(screen.getByRole('button', { name: '重新读取日志' }));

    expect(await screen.findByText('recovered output')).toBeVisible();
    expect(screen.getByText(/运行中 ·/)).toBeVisible();
    expect(attempts).toBe(2);
  });

  it('announces loading while the authoritative list is pending', () => {
    const pendingList = Promise.race<never>([]);
    const transport = new MockControlTransport({
      routes: {
        'agent.session.backgroundJobs.list': () => pendingList,
      },
    });
    renderJobs(transport, []);

    expect(screen.getByRole('status')).toHaveTextContent('正在读取后台任务');
  });
});

function LiveJobs({
  active = true,
  fallbackJobs,
  onOpenJob,
}: {
  active?: boolean;
  fallbackJobs?: AgentBackgroundJobV1[];
  onOpenJob?: (job: AgentBackgroundJobV1) => void;
}) {
  const transport = useControlTransport();
  const address = agentSessionAddress(transport, sessionId);
  const projection = useAgentLiveStore((state) => selectAgentProjection(state, address));
  const jobs = fallbackJobs ?? (projection
    ? projection.backgroundJobOrder
      .map((jobId) => projection.backgroundJobsById[jobId])
      .filter((job): job is AgentBackgroundJobV1 => Boolean(job))
    : []);
  return <AgentBackgroundJobsView active={active} sessionId={sessionId} jobs={jobs} onOpenJob={onOpenJob} />;
}

function renderJobs(
  transport: MockControlTransport,
  fallbackJobs?: AgentBackgroundJobV1[],
  onOpenJob?: (job: AgentBackgroundJobV1) => void,
  active = true,
): void {
  const address = agentSessionAddress(transport, sessionId);
  if (!selectAgentProjection(useAgentLiveStore.getState(), address)) {
    useAgentLiveStore.getState().hydrateSnapshot(address, {
      messages: [],
      liveEvents: [],
      lastSequence: 4,
      resumeToken: `${sessionId}:4`,
      backgroundJobs: fallbackJobs ?? [runningJob],
    });
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <ControlTransportProvider transport={transport}>
      <QueryClientProvider client={client}>
        <LiveJobs active={active} fallbackJobs={fallbackJobs} onOpenJob={onOpenJob} />
      </QueryClientProvider>
    </ControlTransportProvider>,
  );
}

function backgroundJob(
  status: AgentBackgroundJobV1['status'],
  overrides: Partial<AgentBackgroundJobV1> = {},
): AgentBackgroundJobV1 {
  const terminal = (
    status === 'completed'
    || status === 'failed'
    || status === 'cancelled'
    || status === 'orphaned'
  );
  return {
    schemaVersion: 'rag-ime.agent-background-job.v1',
    jobId: 'bg_0123456789abcdef0123456789abcdef',
    sessionId,
    label: `任务-${status}`,
    status,
    command: 'pnpm build',
    commandSha256: 'a'.repeat(64),
    cwd: '/workspace',
    networkAllowed: false,
    maxRunSeconds: 120,
    pid: status === 'queued' ? null : 42,
    createdAtMs: 100,
    startedAtMs: status === 'queued' ? 0 : 110,
    updatedAtMs: 120,
    endedAtMs: terminal ? 180 : 0,
    exitCode: status === 'completed'
      ? 0
      : status === 'failed'
        ? 1
        : status === 'cancelled'
          ? 143
          : null,
    outputBytes: 0,
    logStartCursor: 0,
    logTruncated: false,
    cancelRequestedAtMs: status === 'cancelling' || status === 'cancelled' ? 150 : 0,
    error: status === 'failed' ? '命令退出码为 1' : '',
    approvalId: 'approval-1',
    causalMetadata: {
      todoId: 'todo-1',
      todoRevision: 1,
      goalId: 'goal-1',
      goalRevision: 1,
      turnId: 'turn-1',
      roomBound: false,
    },
    ...overrides,
  };
}

function jobList(items: AgentBackgroundJobV1[]) {
  return {
    schemaVersion: 'rag-ime.agent-background-job-list.v1',
    ok: true,
    sessionId,
    items,
    activeCount: items.filter((job) => (
      job.status === 'queued' || job.status === 'running' || job.status === 'cancelling'
    )).length,
  };
}

function logResponse(job: AgentBackgroundJobV1, text = '') {
  return {
    schemaVersion: 'rag-ime.agent-background-job-log.v1',
    ok: true,
    jobId: job.jobId,
    sessionId,
    cursor: job.logStartCursor,
    nextCursor: job.logStartCursor + new TextEncoder().encode(text).byteLength,
    logStartCursor: job.logStartCursor,
    truncatedBeforeCursor: false,
    hasMore: false,
    text,
  };
}
