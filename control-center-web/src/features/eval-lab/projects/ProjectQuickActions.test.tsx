import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import type { AgentBackgroundJobV1 } from '@/contracts/generated/agent-background-job.v1';
import { PawOsDesktopProvider } from '@/features/paw-os/surface-context';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { loopbackPreviewUrlFromLogs, ProjectQuickActions, projectQuickActionCommand, isActiveProjectQuickAction } from './ProjectQuickActions';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const sessionId = 'agent:room-coordinator';
const context = {
  projectId: 'room:48e5dd64-18df-466a-84a9-d7603fd499e9',
  title: 'Room 项目结果',
  sessionId,
  cwd: '/Volumes/undo 4t/gametest/paw-room-sol61-20260930',
  previewUrl: 'http://127.0.0.1:5392/',
};

function job(status: AgentBackgroundJobV1['status'], command: string): AgentBackgroundJobV1 {
  return {
    schemaVersion: 'rag-ime.agent-background-job.v1',
    jobId: 'bg_0123456789abcdef0123456789abcdef',
    sessionId,
    label: '项目预览',
    status,
    command,
    commandSha256: 'a'.repeat(64),
    cwd: context.cwd,
    networkAllowed: false,
    maxRunSeconds: 3_600,
    pid: 123,
    createdAtMs: 1,
    startedAtMs: 1,
    updatedAtMs: 1,
    endedAtMs: 0,
    exitCode: null,
    outputBytes: 0,
    logStartCursor: 0,
    logTruncated: false,
    cancelRequestedAtMs: 0,
    error: '',
    approvalId: '',
    causalMetadata: { todoId: '', todoRevision: 0, goalId: '', goalRevision: 0, turnId: '', roomBound: false },
  };
}

function renderActions(transport: MockControlTransport, openWindow = vi.fn(), active = true) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <ControlTransportProvider transport={transport}>
      <QueryClientProvider client={queryClient}>
        <PawOsDesktopProvider openWindow={openWindow}>
          <ProjectQuickActions active={active} context={context} />
        </PawOsDesktopProvider>
      </QueryClientProvider>
    </ControlTransportProvider>,
  );
  return openWindow;
}

describe('ProjectQuickActions', () => {
  it('discovers a loopback Browser URL from the owned job stdout without assuming a port or path', () => {
    expect(loopbackPreviewUrlFromLogs('Local skeleton: http://127.0.0.1:8787/ (root: /tmp/project)')).toBe('http://127.0.0.1:8787/');
    expect(loopbackPreviewUrlFromLogs('ready at https://example.test/')).toBe('');
  });

  it('sends a structured checks action and opens the durable process window', async () => {
    const started = job('running', projectQuickActionCommand('checks'));
    const transport = new MockControlTransport({
      routes: {
        'agent.session.backgroundJobs.list': {
          schemaVersion: 'rag-ime.agent-background-job-list.v1', ok: true, sessionId, items: [], activeCount: 0,
        },
        'agent.session.backgroundJob.start': (request: ControlRequest) => {
          expect(request.body).toMatchObject({ action: 'checks', projectId: context.projectId, cwd: context.cwd });
          return { ok: true, job: started, quickAction: { action: 'checks', script: 'test' } };
        },
      },
    });
    const openWindow = renderActions(transport);
    await userEvent.setup().click(screen.getByRole('button', { name: '运行检核' }));
    await waitFor(() => expect(openWindow).toHaveBeenCalledWith(expect.objectContaining({ appId: 'terminal' })));
    expect(transport.requests.some((call) => call.request.pathId === 'agent.session.backgroundJob.start')).toBe(true);
  });

  it('reuses an active preview job and opens Browser only for an explicit loopback URL', async () => {
    const active = job('running', projectQuickActionCommand('preview'));
    const transport = new MockControlTransport({
      routes: {
        'agent.session.backgroundJobs.list': {
          schemaVersion: 'rag-ime.agent-background-job-list.v1', ok: true, sessionId, items: [active], activeCount: 1,
        },
      },
    });
    const openWindow = renderActions(transport);
    await userEvent.setup().click(await screen.findByRole('button', { name: '运行预览' }));
    expect(transport.requests.some((call) => call.request.pathId === 'agent.session.backgroundJob.start')).toBe(false);
    expect(openWindow).toHaveBeenCalledWith(expect.objectContaining({ appId: 'terminal' }));
    expect(openWindow).toHaveBeenCalledWith(expect.objectContaining({ appId: 'browser', target: expect.objectContaining({ url: context.previewUrl }) }));
  });

  it('reuses the bound Room preview with explicit script arguments', async () => {
    const active = job('running', 'npm run start -- --port 8787');
    const transport = new MockControlTransport({ routes: {
      'agent.session.backgroundJobs.list': {
        schemaVersion: 'rag-ime.agent-background-job-list.v1', ok: true, sessionId, items: [active], activeCount: 1,
      },
    } });
    const openWindow = renderActions(transport);
    await screen.findByText('后台任务运行中');
    await userEvent.setup().click(screen.getByRole('button', { name: '运行预览' }));
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.backgroundJob.start')).toBe(false);
    expect(openWindow).toHaveBeenCalledWith(expect.objectContaining({ appId: 'terminal' }));
    expect(openWindow).toHaveBeenCalledWith(expect.objectContaining({ appId: 'browser' }));
  });

  it('matches only a single declared preview command and keeps checks exact', () => {
    expect(isActiveProjectQuickAction(job('running', 'npm run start -- --port 8787'), 'preview')).toBe(true);
    for (const command of ['npm run startup', 'npm run start -- --port 8787; echo other', 'npm run start\necho other']) {
      expect(isActiveProjectQuickAction(job('running', command), 'preview')).toBe(false);
    }
    expect(isActiveProjectQuickAction(job('running', 'npm run test -- tests/unit.test.js'), 'checks')).toBe(false);
    expect(isActiveProjectQuickAction({ ...job('running', 'npm run start -- --port 8787'), cwd: '/tmp/another-project' }, 'preview', context.cwd)).toBe(false);
  });

  it('does not poll background jobs for an inactive owning mount', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.session.backgroundJobs.list': {
        schemaVersion: 'rag-ime.agent-background-job-list.v1', ok: true, sessionId, items: [], activeCount: 0,
      },
    } });
    renderActions(transport, vi.fn(), false);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(transport.requests).toHaveLength(0);
  });

  it('pauses hidden polling and resumes after the document becomes visible', async () => {
    let visibility: DocumentVisibilityState = 'hidden';
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
    const transport = new MockControlTransport({ routes: {
      'agent.session.backgroundJobs.list': {
        schemaVersion: 'rag-ime.agent-background-job-list.v1', ok: true, sessionId, items: [], activeCount: 0,
      },
    } });
    renderActions(transport);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(transport.requests).toHaveLength(0);

    visibility = 'visible';
    document.dispatchEvent(new Event('visibilitychange'));
    await waitFor(() => expect(transport.requests.map(({ request }) => request.pathId)).toEqual([
      'agent.session.backgroundJobs.list',
    ]));
  });
});
