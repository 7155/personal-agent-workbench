import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { projectWayfinderWork } from './wayfinder-work-projection';
import { PawWorkDirectoryProvider, usePawWorkDirectory } from './PawWorkDirectory';

afterEach(() => {
  cleanup();
  setDocumentVisibility('visible');
  vi.useRealTimers();
});

describe('PawWorkDirectoryProvider', () => {
  it('leaves the startup lane to the foreground App before polling shell projections', async () => {
    vi.useFakeTimers({ now: 1_000 });
    const transport = directoryTransport();
    renderDirectory(transport, {
      initialPollDelayMs: 1_200,
      maintenancePollIntervalMs: 30_000,
      pollIntervalMs: 7_500,
    });
    await flushRequests();
    expect(transport.requests).toHaveLength(0);

    await act(async () => { await vi.advanceTimersByTimeAsync(1_199); });
    expect(transport.requests).toHaveLength(0);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(requestCount(transport, 'agent.sessions.list')).toBe(1);
    expect(requestCount(transport, 'agent.rooms.list')).toBe(1);
    expect(requestCount(transport, 'agent.memoryMaintenance.run')).toBe(1);
  });

  it('polls the lightweight directory without repeating the heavy maintenance status cadence', async () => {
    vi.useFakeTimers({ now: 1_000 });
    const transport = directoryTransport();
    renderDirectory(transport, { maintenancePollIntervalMs: 30_000, pollIntervalMs: 7_500 });
    await flushRequests();

    expect(requestCount(transport, 'agent.sessions.list')).toBe(1);
    expect(requestCount(transport, 'agent.rooms.list')).toBe(1);
    expect(requestCount(transport, 'agent.memoryMaintenance.run')).toBe(1);
    expect(transport.requests.find(({ request }) => request.pathId === 'agent.sessions.list')?.request.query).toEqual({
      limit: 100,
      projectionOnly: 1,
    });
    expect(transport.requests.find(({ request }) => request.pathId === 'agent.rooms.list')?.request.query).toEqual({
      limit: 100,
      projectionOnly: 1,
    });
    expect(transport.requests.find(({ request }) => request.pathId === 'agent.memoryMaintenance.run')?.request.query).toEqual({
      limit: 1,
      projectionOnly: 1,
    });

    await act(async () => { await vi.advanceTimersByTimeAsync(7_500); });
    expect(requestCount(transport, 'agent.sessions.list')).toBe(2);
    expect(requestCount(transport, 'agent.rooms.list')).toBe(2);
    expect(requestCount(transport, 'agent.memoryMaintenance.run')).toBe(1);
  });

  it('uses a 30 second passive directory cadence by default', async () => {
    vi.useFakeTimers({ now: 1_000 });
    const transport = directoryTransport();
    renderDirectory(transport, { maintenancePollIntervalMs: 60_000 });
    await flushRequests();
    expect(requestCount(transport, 'agent.sessions.list')).toBe(1);

    await act(async () => { await vi.advanceTimersByTimeAsync(29_999); });
    expect(requestCount(transport, 'agent.sessions.list')).toBe(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(requestCount(transport, 'agent.sessions.list')).toBe(2);
  });

  it('cancels while hidden and refreshes exactly once when visibility returns', async () => {
    vi.useFakeTimers({ now: 1_000 });
    const transport = directoryTransport();
    renderDirectory(transport, { maintenancePollIntervalMs: 60_000 });
    await flushRequests();
    expect(requestCount(transport, 'agent.sessions.list')).toBe(1);

    act(() => setDocumentVisibility('hidden'));
    expect(screen.getByTestId('directory-state')).toHaveTextContent('stale:');
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(requestCount(transport, 'agent.sessions.list')).toBe(1);
    fireEvent.click(screen.getByRole('button', { name: '刷新目录状态' }));
    await flushRequests();
    expect(requestCount(transport, 'agent.sessions.list')).toBe(1);

    act(() => setDocumentVisibility('visible'));
    expect(screen.getByTestId('directory-state')).toHaveTextContent('stale:');
    await flushRequests();
    expect(screen.getByTestId('directory-state')).toHaveTextContent('fresh:');
    expect(requestCount(transport, 'agent.sessions.list')).toBe(2);
    expect(requestCount(transport, 'agent.rooms.list')).toBe(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(29_999); });
    expect(requestCount(transport, 'agent.sessions.list')).toBe(2);
  });

  it('aborts in-flight directory reads immediately when the page becomes inactive', async () => {
    vi.useFakeTimers({ now: 1_000 });
    const signals: AbortSignal[] = [];
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': (request: ControlRequest) => {
        if (request.signal) signals.push(request.signal);
        return new Promise(() => undefined);
      },
      'agent.rooms.list': (request: ControlRequest) => {
        if (request.signal) signals.push(request.signal);
        return new Promise(() => undefined);
      },
      'agent.memoryMaintenance.run': { ok: true, projection: { running: false } },
    } });
    renderDirectory(transport, { maintenancePollIntervalMs: 60_000 });
    await flushRequests();
    expect(signals).toHaveLength(2);
    expect(signals.every((signal) => !signal.aborted)).toBe(true);

    setDocumentVisibility('hidden');

    expect(signals.every((signal) => signal.aborted)).toBe(true);
  });

  it('keeps explicit refresh immediate without turning failures into a tight retry loop', async () => {
    vi.useFakeTimers({ now: 1_000 });
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': () => { throw new Error('offline'); },
      'agent.rooms.list': () => { throw new Error('offline'); },
      'agent.memoryMaintenance.run': { ok: true, projection: { running: false } },
    } });
    renderDirectory(transport, { maintenancePollIntervalMs: 60_000 });
    await flushRequests();
    expect(requestCount(transport, 'agent.sessions.list')).toBe(1);

    await act(async () => { await vi.advanceTimersByTimeAsync(29_999); });
    expect(requestCount(transport, 'agent.sessions.list')).toBe(1);
    fireEvent.click(screen.getByRole('button', { name: '刷新目录状态' }));
    await flushRequests();
    expect(requestCount(transport, 'agent.sessions.list')).toBe(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(29_999); });
    expect(requestCount(transport, 'agent.sessions.list')).toBe(2);
  });

  it('marks expired Runtime provenance offline instead of retaining a stale running state', async () => {
    vi.useFakeTimers({ now: 1_000 });
    let rejectDirectory = false;
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': () => {
        if (rejectDirectory) throw new Error('offline');
        return { ok: true, items: [session('busy')] };
      },
      'agent.rooms.list': () => {
        if (rejectDirectory) throw new Error('offline');
        return { ok: true, items: [] };
      },
      'agent.memoryMaintenance.run': { ok: true, projection: { running: false } },
    } });
    renderDirectory(transport, { maintenancePollIntervalMs: 60_000, pollIntervalMs: 10_000 });
    await flushRequests();
    expect(screen.getByTestId('directory-state')).toHaveTextContent('fresh:进行中');

    rejectDirectory = true;
    vi.setSystemTime(32_000);
    fireEvent.click(screen.getByRole('button', { name: '刷新目录状态' }));
    await flushRequests();
    expect(screen.getByTestId('directory-state')).toHaveTextContent('stale:已离线');
  });

  it('keeps loaded history but marks it offline immediately after a directory sync error', async () => {
    vi.useFakeTimers({ now: 1_000 });
    let rejectDirectory = false;
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': () => {
        if (rejectDirectory) throw new Error('offline');
        return { ok: true, items: [session('busy')] };
      },
      'agent.rooms.list': () => {
        if (rejectDirectory) throw new Error('offline');
        return { ok: true, items: [] };
      },
      'agent.memoryMaintenance.run': { ok: true, projection: { running: false } },
    } });
    renderDirectory(transport, { maintenancePollIntervalMs: 60_000, pollIntervalMs: 10_000 });
    await flushRequests();
    expect(screen.getByTestId('directory-state')).toHaveTextContent('fresh:进行中');

    rejectDirectory = true;
    vi.setSystemTime(2_000);
    fireEvent.click(screen.getByRole('button', { name: '刷新目录状态' }));
    await flushRequests();

    expect(screen.getByTestId('directory-state')).toHaveTextContent('stale:已离线');
  });

  it('drops an old space projection before a replacement transport can resolve', async () => {
    const oldSession = deferred<{ ok: true; items: ReturnType<typeof session>[] }>();
    const oldTransport = new MockControlTransport({ routes: {
      'agent.sessions.list': () => oldSession.promise,
      'agent.rooms.list': { ok: true, items: [] },
      'agent.memoryMaintenance.run': { ok: true, projection: { running: false } },
    } });
    const nextTransport = new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [{ ...session('idle'), id: 'session-project' }] },
      'agent.rooms.list': { ok: true, items: [] },
      'agent.memoryMaintenance.run': { ok: true, projection: { running: false } },
    } });
    const view = renderDirectory(oldTransport, { initialPollDelayMs: 0, maintenancePollIntervalMs: 60_000 });
    await flushRequests();
    expect(requestCount(oldTransport, 'agent.sessions.list')).toBe(1);

    view.rerender(
      <ControlTransportProvider transport={nextTransport}>
        <PawWorkDirectoryProvider initialPollDelayMs={0} maintenancePollIntervalMs={60_000}>
          <DirectoryProbe />
        </PawWorkDirectoryProvider>
      </ControlTransportProvider>,
    );
    await flushRequests();
    expect(requestCount(nextTransport, 'agent.sessions.list')).toBe(1);
    expect(screen.getByTestId('directory-session')).toHaveTextContent('session-project');

    await act(async () => { oldSession.resolve({ ok: true, items: [{ ...session('busy'), id: 'session-personal' }] }); });
    expect(screen.getByTestId('directory-session')).toHaveTextContent('session-project');
  });
});

function renderDirectory(
  transport: MockControlTransport,
  props: { initialPollDelayMs?: number; maintenancePollIntervalMs: number; pollIntervalMs?: number },
) {
  return render(
    <ControlTransportProvider transport={transport}>
      <PawWorkDirectoryProvider initialPollDelayMs={0} {...props}>
        <DirectoryProbe />
      </PawWorkDirectoryProvider>
    </ControlTransportProvider>,
  );
}

function DirectoryProbe() {
  const directory = usePawWorkDirectory();
  const item = projectWayfinderWork({
    nowMs: Date.now(),
    roomStatusFresh: directory.roomStatusFresh,
    rooms: directory.rooms,
    sessionStatusFresh: directory.sessionStatusFresh,
    sessions: directory.sessions,
  }).projects[0]?.items[0];
  return (
    <>
      <output data-testid="directory-state">
        {directory.sessionStatusFresh && directory.roomStatusFresh ? 'fresh' : 'stale'}:{item?.statusLabel ?? 'none'}
      </output>
      <output data-testid="directory-session">{directory.sessions[0]?.id ?? 'none'}</output>
      <button onClick={() => void directory.refresh()} type="button">刷新目录状态</button>
    </>
  );
}

function directoryTransport() {
  return new MockControlTransport({ routes: {
    'agent.sessions.list': { ok: true, items: [session('busy')] },
    'agent.rooms.list': { ok: true, items: [] },
    'agent.memoryMaintenance.run': { ok: true, projection: { running: false } },
  } });
}

function session(status: string) {
  return {
    id: 'session-1',
    title: '真实运行对话',
    mode: 'assistant',
    status,
    roleId: 'default',
    roleVersion: '1',
    roleBookRevisionId: 'r1',
    updatedAtMs: 1,
    workspaceRoots: ['/work/paw'],
  };
}

function requestCount(transport: MockControlTransport, pathId: string): number {
  return transport.requests.filter(({ request }) => request.pathId === pathId).length;
}

async function flushRequests() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => { resolve = nextResolve; });
  return { promise, resolve };
}

function setDocumentVisibility(state: 'hidden' | 'visible'): void {
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    value: state,
  });
  document.dispatchEvent(new Event('visibilitychange'));
}
